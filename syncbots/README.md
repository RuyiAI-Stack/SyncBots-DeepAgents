# SyncBots (Deep Agent edition)

Loop-engineered, deep-agent based LLVM/MLIR automatic upgrade tool. A full
rewrite of the original LangGraph + claude-code pipeline on top of the
[`deepagents`](https://docs.langchain.com/oss/python/deepagents) subagent
framework. The `claude` CLI is no longer used.

## Architecture

Two layers:

- **Loop engineering** (`syncbots/loop/`): an outer deterministic controller
  that periodically runs the deep agent and, after each run, builds + tests the
  repo. **Unit tests passing is the sole success-exit condition.** On failure it
  diagnoses the error, guards against repeating an identical failure, points the
  agent at the relevant fix-strategy skill, and runs the agent again.
- **Deep agent** (`syncbots/agent/`): built with `create_deep_agent`. The main
  agent only plans (`write_todos`) and edits code, delegating token-heavy work
  to context-isolation subagents:
  - `diff-digest` — reads giant LLVM diffs, returns a structured `DiffDigest`.
  - `log-analyst` — reads long build logs, returns a structured `LogDiagnosis`.
  - `check-regen` — runs opt tools to regenerate failing FileCheck lines.

Structured subagent outputs use `response_format` (pydantic), replacing the
original project's regex-based parsing. There is no static knowledge base:
breaking-change patterns are derived at runtime by the `diff-digest` subagent
reading the actual LLVM diff, then grep-verified.

Cross-iteration / cross-repo learnings are persisted at two levels
(`~/.syncbots/`):

- **Curated store** (`memory.jsonl` -> rendered `MEMORY.md`): after each run
  the controller archives structured entries (repo, LLVM range, error type,
  lesson). PASS runs produce *verified* entries; aborted runs produce negative
  "do not repeat" entries. Entries are deduplicated by normalized lesson, and
  rendering prunes them (per-error-type caps, verified preferred, stale
  unverified entries dropped) so the injected memory stays small and current.
- **Free-form memory** (`AGENTS.md`): agent-editable via `MemoryMiddleware`,
  automatically trimmed when oversized (newest content kept).

Fix playbooks live as **per-error-type skills** in `syncbots/skills/`
(`compile-error-fix`, `linker-error-fix`, `tablegen-error-fix`,
`cmake-error-fix`, `test-failure-fix`, `unknown-error-triage`), loaded via
`SkillsMiddleware` with progressive disclosure — the agent sees only
names/descriptions up front, and each fix iteration names the ONE skill
matching the diagnosed error type, so the agent reads a single small
`SKILL.md` instead of a monolithic playbook. The system prompts are kept
lean; the detailed strategies are not inlined or injected by the controller.

Code retrieval follows a token-saving **three-layer protocol** (documented in
the `code-search` skill): `glob` (which files exist) -> `grep` (where a symbol
is used) -> `read_file` (only the region to be edited, paged with
offset/limit). The agent escalates layers only on demand.

```
prepare (restore -> pull -> submodules) -> scan (deterministic)
  -> plan segments -> FOR EACH segment {
       prescan (grep) -> LOOP { deep agent -> verify }
     } -> result
```

**Staged upgrades**: when the LLVM first-parent span exceeds `--segment-span`
commits (default 1000), the range is automatically split into intermediate
segment targets and upgraded one segment at a time -- each segment gets its
own (smaller) diff digest, prescan, and fix loop, and the anchor advances only
after the segment's tests pass. This keeps failures attributable on huge
version jumps. `--segment-span 0` disables staging; `max_iterations` applies
per segment.

Before any upgrade work, `prepare_repo` restores the downstream repo to a
pristine default state (abort merges/rebases, `reset --hard`, `clean -fd`),
fetches the latest refs (fast-forwarding the current branch, or checking out
`base_ref` when configured), and updates all git submodules recursively —
only then is the LLVM anchor bumped and the loop started.

## Install

```bash
cd SyncBotsDeep
pip install -e .
```

Dependencies: `deepagents>=0.6`, `langgraph>=1.0`, `langchain>=1.0`, plus the
usual langchain provider packages.

## Configure LLMs

### Strong/weak model tiering (recommended)

Like Claude Code, SyncBots routes roles across two model tiers to save cost:
the **main editing agent** gets the strong model, while the token-heavy
subagent roles (`diff-digest`, `log-analyst`, `check-regen`) get the weak
(cheap) model. Configure once via YAML:

```yaml
default:
  provider: anthropic
  api_key: <key>

strong_model: claude-opus-4-8    # main agent (reasoning + editing)
weak_model: claude-haiku-4-5     # diff-digest / log-analyst / check-regen
```

or inline on any run command:

```bash
syncbots upgrade-single --repo stablehlo --repo-path ./stablehlo \
  --strong-model claude-opus-4-8 --weak-model claude-haiku-4-5 \
  --target-llvm <llvm-commit>
```

Resolution priority per role: `--model` (global override) > explicit per-role
config > tier model > `default.model`.

There are three further ways to configure the API, in increasing convenience:

### 1. Interactive wizard (easiest)

```bash
syncbots init
```

Walks you through API config (provider / base URL / key / model), repo name,
repo path, and target version, optionally saves an `llm_config.yaml`, then runs
the upgrade.

### 2. Inline API flags (no YAML editing)

Every run command accepts API overrides directly. They apply to all agent roles
and override any `llm_config.yaml`:

```bash
syncbots upgrade-single --repo stablehlo --repo-path ./stablehlo \
  --provider openai_compatible \
  --base-url https://api.example.com/v1 \
  --api-key sk-xxxx \
  --model claude-sonnet-4-6 \
  --target-llvm <llvm-commit>
```

Flags: `--provider` (`anthropic` | `openai` | `openai_compatible`),
`--base-url`, `--api-key`, `--model`.

### 3. `llm_config.yaml` (per-role control)

`llm_config.yaml` supports per-role config. Role names map to the new topology
(`main`, `diff_digest`, `log_analyst`, `check_regen`); legacy names (`coder`,
`analyzer`, `reporter`) are accepted as aliases. The legacy
`coder.backend: claudecode` field is ignored. Inline flags above override these.

```yaml
default:
  provider: anthropic
  model: claude-sonnet-4-6
  api_key: <key>
  base_url: https://api.example.com/v1

main:
  model: claude-opus-4-6   # strong model for the editing agent
diff_digest:
  model: claude-sonnet-4-6 # cheap long-context model for digesting diffs
```

## Usage

```bash
# Analyze only (scan + grep prescan, no code changes)
syncbots analyze --repos-dir . --repos stablehlo --target-hash <llvm-commit>

# Single-repo upgrade with auto branch + PR
syncbots upgrade-single --repo stablehlo --repo-path ./stablehlo \
  --target-llvm <llvm-commit> --auto-branch --auto-pr

# Full multi-repo loop
syncbots upgrade --repos-dir . --target-hash <llvm-commit> --max-iterations 0

# Fixed benchmark cases (tracks pass rate / iterations / tokens over time)
syncbots bench --bench-config bench.yaml
```

`--max-iterations 0` means unlimited; the loop also stops early if the identical
failure recurs (default 3 times).

## Metrics

Every LLM call (main agent and subagents) is metered via a usage callback;
per-iteration and total token counts appear in each run's `summary.md`, the
CLI output, and `UpgradeResult`. The `bench` command runs a fixed set of
`(repo, base_llvm, target_llvm)` cases from a YAML file:

```yaml
cases:
  - name: buddy-19-to-20
    repo: buddy-mlir
    repo_path: ../buddy-mlir
    base_ref: main            # optional
    base_llvm: <hash>         # optional
    target_llvm: <hash>
    max_iterations: 5
```

and appends one JSON line per case plus a summary line (pass rate, average
iterations to pass, token totals) to `<output>/bench/bench-results.jsonl`, so
prompt/skill changes can be compared quantitatively across runs.

## Tests

```bash
python -m pytest syncbots/tests/ -q
```

Unit tests cover the verifier (error classification/condensing), the controller
(exit conditions, de-duplication), and prescan (grep verification). Full LLVM
builds are not run in CI.

The original `llvm_upgrader/` package is kept alongside for reference.
