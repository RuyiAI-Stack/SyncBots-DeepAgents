"""CLI entry point for SyncBots.

Subcommands (compatible with the original llvm-upgrader for eval frameworks):
  upgrade         Full multi-repo upgrade loop
  upgrade-single  Single-repo upgrade (eval-framework compatible)
  analyze         Scan + prescan only (no code changes)
"""

from __future__ import annotations

import argparse
import datetime
import logging
import os
import re
import subprocess
import sys

from rich.console import Console
from rich.logging import RichHandler

console = Console()


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=False)],
    )


def _load_llm_config(path):
    from .llm import apply_github_token, load_llm_config, set_loaded_config

    try:
        cfg = load_llm_config(path)
    except FileNotFoundError:
        cfg = None
    if cfg is not None:
        apply_github_token(cfg)
        set_loaded_config(cfg)
    return cfg


def _apply_inline_api(cfg, args):
    """Apply inline API overrides (--provider/--base-url/--api-key/--model) to config.

    Lets users customize the API entirely from the command line, without editing
    llm_config.yaml. The overrides apply to the default config and every
    configured role, so a single custom endpoint covers all agents.
    Returns an LLMConfigSet (creating an empty one if no YAML was found).
    """
    from .llm import AgentLLMConfig, LLMConfigSet, set_loaded_config

    provider = getattr(args, "provider", None)
    base_url = getattr(args, "base_url", None)
    api_key = getattr(args, "api_key", None)
    model = getattr(args, "model", "auto")
    inline_model = model if model and model != "auto" else None
    strong_model = getattr(args, "strong_model", None)
    weak_model = getattr(args, "weak_model", None)

    if not any([provider, base_url, api_key, inline_model, strong_model, weak_model]):
        return cfg  # nothing to override

    if cfg is None:
        cfg = LLMConfigSet()

    # Strong/weak tier overrides (Claude Code style): main agent gets the
    # strong model, subagent roles get the weak one (see llm.ROLE_MODEL_TIER).
    if strong_model:
        cfg.strong_model = strong_model
    if weak_model:
        cfg.weak_model = weak_model

    targets = [cfg.default] + list(cfg.roles.values())
    if not cfg.roles:
        targets = [cfg.default]
    for c in targets:
        if provider:
            c.provider = provider
        if base_url is not None:
            c.base_url = base_url
        if api_key:
            c.api_key = api_key
        if inline_model:
            c.model = inline_model

    set_loaded_config(cfg)
    return cfg


def _agent_run_kwargs(args: argparse.Namespace) -> dict:
    kwargs = {
        "show_agent": getattr(args, "show_agent", False),
        "agent_log_dir": getattr(args, "agent_log_dir", None) or None,
        "output_dir": getattr(args, "output_dir", None) or None,
    }
    segment_span = getattr(args, "segment_span", None)
    if segment_span is not None:
        kwargs["segment_max_span"] = int(segment_span)
    return kwargs


def _print_agent_reports(result) -> None:
    """Print where agent analysis was saved and per-iteration summaries."""
    if not result.log_dir:
        return
    console.print(f"\n[bold cyan]Agent analysis logs:[/bold cyan] {result.log_dir}")
    console.print(f"  summary: {result.log_dir}/summary.md")
    tokens = result.token_totals()
    if tokens["total_tokens"]:
        console.print(
            f"  tokens: in={tokens['input_tokens']} out={tokens['output_tokens']} "
            f"total={tokens['total_tokens']}  ({result.duration_sec:.0f}s)"
        )
    for report in result.agent_reports:
        console.print(f"\n[bold]Iteration {report.iteration}[/bold]")
        if report.transcript_path:
            console.print(f"  transcript: {report.transcript_path}")
        preview = (report.summary or "").strip()
        if len(preview) > 600:
            preview = preview[:580] + "..."
        if preview:
            console.print(f"  {preview}")
        if report.error:
            console.print(f"  [yellow]error: {report.error}[/yellow]")


def cmd_upgrade(args: argparse.Namespace) -> int:
    """Full multi-repo upgrade loop."""
    from .config import load_configs
    from .loop.controller import run_upgrade

    repos = load_configs(repos_dir=args.repos_dir, config_file=args.config, repo_filter=args.repos)
    if not repos:
        console.print("[red]No repositories found to upgrade.[/red]")
        return 1

    for r in repos:
        if args.base_ref and not r.base_ref:
            r.base_ref = args.base_ref
        if args.base_llvm and not r.base_llvm:
            r.base_llvm = args.base_llvm

    console.print(f"[bold]SyncBots[/bold] - {len(repos)} repo(s) to process")
    for r in repos:
        console.print(f"  - {r.repo_name} ({r.local_path})")

    llm_config = _load_llm_config(args.llm_config)
    llm_config = _apply_inline_api(llm_config, args)
    override_model = args.model if args.model != "auto" else None

    results = []
    for repo in repos:
        result = run_upgrade(
            repo, target_llvm_hash=args.target_hash or "",
            max_iterations=args.max_iterations, llm_config=llm_config,
            override_model=override_model, enable_memory=not args.no_memory,
            **_agent_run_kwargs(args),
        )
        results.append(result)
        _print_agent_reports(result)

    _print_summary(results)
    return 0 if all(r.status in ("pass", "skipped") for r in results) else 1


def cmd_upgrade_single(args: argparse.Namespace) -> int:
    """Single-repo upgrade (eval-framework compatible)."""
    from .config import load_configs
    from .loop.controller import run_upgrade
    from .repo_config import KNOWN_LLVM_ANCHORS, discover_repo_config, load_builtin_config, yaml_to_repo_info
    from .state import RepoInfo

    repo = _resolve_single_repo(args, KNOWN_LLVM_ANCHORS, load_configs,
                                discover_repo_config, load_builtin_config,
                                yaml_to_repo_info, RepoInfo)
    if repo is None:
        console.print(f"[red]Repo not found: {args.repo}[/red]")
        return 1

    ok, msg = _validate_repo_path(repo.local_path, repo.repo_name)
    if not ok:
        console.print(f"[red]{msg}[/red]")
        return 1

    if args.base_ref:
        repo.base_ref = args.base_ref
    if args.base_llvm:
        repo.base_llvm = args.base_llvm

    console.print(f"[bold]Single-repo upgrade: {repo.repo_name}[/bold]")

    auto_branch_name = ""
    if args.auto_branch:
        branch_base = args.branch_base or repo.base_ref or "bump-llvm"
        target_hint = re.sub(r"[^0-9A-Za-z._-]+", "-", (args.target_llvm or "llvm-main"))[:12].strip("-") or "llvm-main"
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        auto_branch_name = f"upgrade/{branch_base}-{target_hint}-{ts}"
        ok, msg = _prepare_branch(repo.local_path, branch_base, auto_branch_name, args.branch_remote)
        if not ok:
            console.print(f"[red]{msg}[/red]")
            return 1
        console.print(f"[green]{msg}[/green]")

    llm_config = _load_llm_config(args.llm_config)
    llm_config = _apply_inline_api(llm_config, args)
    override_model = args.model if args.model != "auto" else None

    result = run_upgrade(
        repo, target_llvm_hash=args.target_llvm or "",
        max_iterations=args.max_iterations, llm_config=llm_config,
        override_model=override_model, enable_memory=not args.no_memory,
        **_agent_run_kwargs(args),
    )

    _print_agent_reports(result)

    if result.status == "pass":
        target_short = (result.target_llvm_hash or args.target_llvm or "unknown")[:12]
        pr_title = f"[LLVM] Integrate LLVM at {target_short}"
        console.print("[bold green]Upgrade successful![/bold green]")
        if args.auto_pr and auto_branch_name:
            ok, msg = _submit_pr(repo.local_path, auto_branch_name,
                                 args.pr_base or args.branch_base, args.branch_remote,
                                 pr_title, f"Automated LLVM upgrade to `{target_short}`.")
            console.print(f"[bold green]PR created:[/bold green] {msg}" if ok
                          else f"[yellow]PR creation failed: {msg}[/yellow]")
        else:
            console.print(f"  Suggested PR title: {pr_title}")
        return 0

    console.print(f"[bold red]Upgrade failed: {result.status}[/bold red]")
    if result.error_message:
        console.print(f"  {result.error_message}")
    return 1


def cmd_analyze(args: argparse.Namespace) -> int:
    """Scan + prescan only (no code changes)."""
    from .config import load_configs
    from .digest import extract_api_change_patterns
    from .prescan import format_prescan_report, grep_repo
    from .scan import scan_repo

    repos = load_configs(repos_dir=args.repos_dir, config_file=args.config, repo_filter=args.repos)
    if not repos:
        console.print("[red]No repositories found.[/red]")
        return 1

    for r in repos:
        if args.base_ref and not r.base_ref:
            r.base_ref = args.base_ref
        if args.base_llvm and not r.base_llvm:
            r.base_llvm = args.base_llvm

    llm_config = _load_llm_config(args.llm_config)
    llm_config = _apply_inline_api(llm_config, args)
    override_model = args.model if args.model != "auto" else None

    for repo in repos:
        scan_repo(repo, target_hash=args.target_hash or "")
        console.print(f"\n[bold]{repo.repo_name}[/bold]: "
                      f"{repo.current_llvm_hash[:12] or '?'} -> {repo.target_llvm_hash[:12] or '?'} "
                      f"({repo.upgrade_type}) [{repo.status}]")
        if repo.status == "pending":
            patterns = extract_api_change_patterns(repo, llm_config, override_model)
            if patterns:
                report = format_prescan_report(grep_repo(repo, patterns))
                console.print(report or "  (prescan found no affected locations)")
            else:
                console.print("  (diff-digest produced no patterns; "
                              "the loop's main agent will analyze during upgrade)")
    return 0


def cmd_bench(args: argparse.Namespace) -> int:
    """Run the fixed benchmark cases and track pass rate / iterations / tokens."""
    from .bench import (
        RESULTS_FILENAME,
        append_records,
        case_record,
        format_report,
        load_bench_config,
        summarize_records,
    )
    from .config import load_configs
    from .loop.controller import run_upgrade
    from .paths import default_output_root
    from .repo_config import KNOWN_LLVM_ANCHORS, discover_repo_config, load_builtin_config, yaml_to_repo_info
    from .state import RepoInfo

    try:
        cases = load_bench_config(args.bench_config)
    except (OSError, ValueError) as e:
        console.print(f"[red]Bad benchmark config: {e}[/red]")
        return 1

    llm_config = _load_llm_config(args.llm_config)
    llm_config = _apply_inline_api(llm_config, args)
    override_model = args.model if args.model != "auto" else None

    console.print(f"[bold]SyncBots benchmark[/bold] - {len(cases)} case(s)")
    records = []
    for case in cases:
        ns = argparse.Namespace(
            repo=case["repo"], repo_path=case.get("repo_path"),
            repo_config=case.get("repo_config"),
            repos_dir=args.repos_dir, config=None,
        )
        repo = _resolve_single_repo(ns, KNOWN_LLVM_ANCHORS, load_configs,
                                    discover_repo_config, load_builtin_config,
                                    yaml_to_repo_info, RepoInfo)
        if repo is None:
            console.print(f"[red]  case '{case['name']}': repo not found, skipping[/red]")
            continue
        if case.get("base_ref"):
            repo.base_ref = case["base_ref"]
        if case.get("base_llvm"):
            repo.base_llvm = case["base_llvm"]

        console.print(f"\n[bold cyan]Case: {case['name']}[/bold cyan] ({repo.repo_name})")
        result = run_upgrade(
            repo, target_llvm_hash=case["target_llvm"],
            max_iterations=int(case["max_iterations"]),
            llm_config=llm_config, override_model=override_model,
            enable_memory=not args.no_memory,
            **_agent_run_kwargs(args),
        )
        records.append(case_record(case["name"], result))

    if not records:
        console.print("[red]No benchmark case could be run.[/red]")
        return 1

    summary = summarize_records(records)
    out_root = args.output_dir or default_output_root()
    results_path = os.path.join(out_root, "bench", RESULTS_FILENAME)
    append_records(results_path, records + [summary])

    console.print(format_report(records, summary))
    console.print(f"\n  results appended to: {results_path}")
    return 0 if summary["passed"] == summary["cases"] else 1


def cmd_init(args: argparse.Namespace) -> int:
    """Interactive wizard: configure the API, pick a repo + path, then run.

    Prompts the user step by step so no manual YAML editing is needed. Can
    optionally save the API config to llm_config.yaml and launch the upgrade.
    """
    from .repo_config import KNOWN_LLVM_ANCHORS

    console.print("[bold]SyncBots setup wizard[/bold]\n")

    # ── 1. API configuration ──
    console.print("[bold cyan]Step 1/4 - LLM API[/bold cyan]")
    console.print("  Select provider:")
    console.print("    [1] anthropic")
    console.print("    [2] openai")
    console.print("    [3] openai_compatible")
    provider_choice = _prompt("Choose provider", default="1")
    provider = {"1": "anthropic", "2": "openai", "3": "openai_compatible"}.get(
        provider_choice.strip(), provider_choice.strip()
    )
    console.print(f"  -> {provider}")

    base_url = _prompt("API base URL (blank for provider default)", default="")
    api_key = _prompt("API key", default="")
    if api_key:
        masked = api_key[:6] + "..." + api_key[-4:] if len(api_key) > 12 else "***"
        console.print(f"  -> key: {masked}")

    # ── 2. Model configuration ──
    console.print("\n[bold cyan]Step 2/4 - Models[/bold cyan]")
    console.print("  Strong model: used by the main editing agent (reasoning + code changes)")
    console.print("  Weak model: used by subagents (diff-digest, log-analyst, check-regen)")
    console.print("")
    console.print("  Common choices:")
    console.print("    Strong: claude-opus-4-8, claude-sonnet-4-6, gpt-4o")
    console.print("    Weak:   claude-haiku-4-5, claude-sonnet-4-6, gpt-4o-mini")
    console.print("")
    strong_model = _prompt("Strong model (main agent)", default="claude-sonnet-4-6")
    weak_model = _prompt("Weak model (subagents, blank = same as strong)", default="")
    if not weak_model:
        weak_model = ""
        console.print("  -> weak model: same as strong")

    # ── 3. Repository ──
    console.print("\n[bold cyan]Step 3/4 - Repository[/bold cyan]")
    known = ", ".join(sorted(a["repo_name"].split("/")[-1] for a in KNOWN_LLVM_ANCHORS))
    console.print(f"  Known repos: {known}")
    repo = _prompt("Repository name (e.g. stablehlo, circt, or your-org/your-repo)")
    default_path = _guess_repo_path(repo)
    repo_path = _prompt("Repository path (absolute or relative)", default=default_path)
    repo_path = os.path.abspath(os.path.expanduser(repo_path))
    ok, msg = _validate_repo_path(repo_path, repo)
    if not ok:
        console.print(f"[yellow]Warning: {msg}[/yellow]")

    # ── 4. Upgrade target ──
    console.print("\n[bold cyan]Step 4/4 - Upgrade target[/bold cyan]")
    target = _prompt("Target LLVM commit hash (blank = LLVM main HEAD)", default="")
    max_iter = _prompt("Max fix iterations (0 = unlimited)", default="0")

    # ── Optionally save the API config ──
    save_path = _prompt(
        "\nSave API config to a file? (path, or blank to skip)",
        default="llm_config.yaml",
    )
    if save_path:
        _write_llm_config(save_path, provider, base_url, api_key, strong_model, weak_model)
        console.print(f"[green]Wrote {save_path}[/green]")

    # ── Build args and dispatch to upgrade-single ──
    if not _confirm("\nStart the upgrade now?", default=True):
        console.print("Setup complete. Run later with:")
        console.print(
            f"  syncbots upgrade-single --repo {repo} --repo-path {repo_path} "
            f"{'--target-llvm ' + target if target else ''} "
            f"{'--llm-config ' + save_path if save_path else ''}".rstrip()
        )
        return 0

    ok, msg = _validate_repo_path(repo_path, repo)
    if not ok:
        console.print(f"[red]Cannot start upgrade: {msg}[/red]")
        return 1

    ns = argparse.Namespace(
        repo=repo, repo_path=repo_path, repo_config=None,
        base_ref=None, base_llvm=None, target_llvm=target or None,
        repos_dir=".", config=None,
        max_iterations=int(max_iter or 0),
        auto_branch=False, auto_pr=False, pr_base="",
        branch_base="bump-llvm", branch_remote="origin",
        llm_config=save_path or None, model="auto",
        strong_model=strong_model or None, weak_model=weak_model or None,
        no_memory=False, provider=provider or None,
        base_url=base_url or None, api_key=api_key or None,
        show_agent=False, agent_log_dir=None, output_dir=None,
        segment_span=None,
    )
    return cmd_upgrade_single(ns)


def _prompt(text: str, default: str | None = None, secret: bool = False) -> str:
    """Prompt the user, returning the entered value or the default."""
    suffix = f" [{default}]" if default else ""
    try:
        if secret:
            import getpass
            val = getpass.getpass(f"{text}{suffix}: ")
        else:
            val = input(f"{text}{suffix}: ")
    except (EOFError, KeyboardInterrupt):
        return default or ""
    return val.strip() or (default or "")


def _confirm(text: str, default: bool = True) -> bool:
    d = "Y/n" if default else "y/N"
    ans = _prompt(f"{text} ({d})", default="").lower()
    if not ans:
        return default
    return ans in ("y", "yes")


def _write_llm_config(path, provider, base_url, api_key, strong_model, weak_model="") -> None:
    """Write a llm_config.yaml with strong/weak model tiering."""
    import yaml

    block: dict = {
        "provider": provider or "anthropic",
        "model": strong_model or "claude-sonnet-4-6",
        "api_key": api_key or "",
    }
    if base_url:
        block["base_url"] = base_url
    data: dict = {"default": dict(block)}
    data["strong_model"] = strong_model or block["model"]
    if weak_model:
        data["weak_model"] = weak_model
    data["github_token"] = ""
    with open(path, "w", encoding="utf-8") as f:
        f.write("# SyncBots LLM config (generated by `syncbots init`)\n")
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)


# ── single-repo resolution helper ────────────────────────────────────────────

def _guess_repo_path(repo_name: str) -> str:
    """Best-effort locate a known downstream repo on disk."""
    short = repo_name.rsplit("/", 1)[-1]
    cwd = os.getcwd()
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(cwd, short),
        os.path.join(cwd, "..", short),
        os.path.join(home, short),
    ]
    for path in candidates:
        path = os.path.abspath(path)
        if os.path.isdir(os.path.join(path, ".git")):
            return path
    return os.path.abspath(os.path.join(cwd, short))


def _validate_repo_path(repo_path: str, repo_name: str = "") -> tuple[bool, str]:
    """Return (ok, message). *repo_path* must exist and be a git checkout."""
    if not repo_path:
        return False, "Repository path is empty."
    if not os.path.isdir(repo_path):
        hint = _guess_repo_path(repo_name) if repo_name else ""
        if hint and hint != repo_path and os.path.isdir(hint):
            return False, f"Repository path does not exist: {repo_path}\n  Did you mean: {hint}"
        return False, f"Repository path does not exist: {repo_path}"
    if not os.path.isdir(os.path.join(repo_path, ".git")):
        return False, f"Not a git repository: {repo_path}"
    return True, ""


def _resolve_single_repo(args, anchors, load_configs, discover_repo_config,
                         load_builtin_config, yaml_to_repo_info, RepoInfo):
    if getattr(args, "repo_config", None):
        from .repo_config import load_repo_yaml
        cfg = load_repo_yaml(args.repo_config)
        return yaml_to_repo_info(cfg, os.path.abspath(args.repo_path or "."))

    if args.repo_path:
        repo_path = os.path.abspath(args.repo_path)
        cfg = discover_repo_config(repo_path)
        if not cfg and args.repo:
            anchor = next((a for a in anchors if args.repo in a["repo_name"]), None)
            if anchor:
                cfg = load_builtin_config(anchor["repo_name"])
        if cfg:
            return yaml_to_repo_info(cfg, repo_path)
        anchor = next((a for a in anchors if args.repo in a["repo_name"]), None)
        if anchor:
            return RepoInfo(
                repo_name=anchor["repo_name"], local_path=repo_path,
                llvm_dep_type=anchor["llvm_dep_type"], llvm_dep_path=anchor["llvm_dep_path"],
                llvm_upstream_github=anchor.get("llvm_upstream_github", ""),
                llvm_upstream_branch=anchor.get("llvm_upstream_branch", ""),
                llvm_upstream_url=anchor.get("llvm_upstream_url", ""),
            )
        return None

    repos = load_configs(repos_dir=args.repos_dir, config_file=args.config,
                         repo_filter=[args.repo] if args.repo else None)
    return repos[0] if repos else None


# ── git helpers for branch/PR ────────────────────────────────────────────────

def _prepare_branch(repo_path, base_branch, new_branch, remote):
    cmds = [
        ["git", "fetch", remote, base_branch],
        ["git", "checkout", base_branch],
        ["git", "pull", "--ff-only", remote, base_branch],
        ["git", "checkout", "-b", new_branch],
    ]
    for cmd in cmds:
        result = subprocess.run(cmd, cwd=repo_path, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            return False, f"Branch setup failed at `{' '.join(cmd)}`: {(result.stderr or result.stdout).strip()[:500]}"
    return True, f"Created branch `{new_branch}` from `{base_branch}`."


def _submit_pr(repo_path, branch, base, remote, title, body):
    for cmd in (["git", "add", "-A"], ["git", "commit", "-m", title], ["git", "push", "-u", remote, branch]):
        result = subprocess.run(cmd, cwd=repo_path, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            return False, f"`{' '.join(cmd)}` failed: {(result.stderr or result.stdout).strip()[:500]}"
    try:
        result = subprocess.run(
            ["gh", "pr", "create", "--base", base, "--head", branch, "--title", title, "--body", body],
            cwd=repo_path, capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        return False, "gh CLI not found."
    if result.returncode != 0:
        return False, f"gh pr create failed: {(result.stderr or result.stdout).strip()[:500]}"
    return True, result.stdout.strip()


def _print_summary(results) -> None:
    console.print("\n[bold]Final Status:[/bold]")
    for r in results:
        icon = {"pass": "[green]PASS[/green]", "skipped": "[dim]SKIP[/dim]"}.get(
            r.status, f"[red]{r.status.upper()}[/red]")
        console.print(f"  {icon}  {r.repo_name} ({r.iterations_used} iter)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="syncbots", description="Deep-agent LLVM/MLIR upgrade tool")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    sub = parser.add_subparsers(dest="command", help="Available commands")

    def _common(p):
        p.add_argument("--llm-config", help="Path to llm_config.yaml")
        p.add_argument("--model", default="auto", help="Override model for all roles ('auto'=config)")
        p.add_argument("--no-memory", action="store_true", help="Disable cross-run agent memory")
        p.add_argument(
            "--show-agent", action="store_true",
            help="Stream agent tool calls and replies to the terminal in real time",
        )
        p.add_argument(
            "--agent-log-dir",
            help="Explicit directory for this run's artifacts (overrides --output-dir)",
        )
        p.add_argument(
            "--output-dir",
            help="Root directory for all logs (default: SyncBotsDeep/output, "
                 "or $SYNCBOTS_OUTPUT_DIR). Layout: <output>/<repo>/<timestamp>/",
        )
        p.add_argument(
            "--segment-span", type=int, default=None,
            help="Max LLVM commits per upgrade segment; larger spans are staged "
                 "into multiple small upgrades (default 1000; 0 disables staging)",
        )
        # Inline API overrides: customize the API without editing llm_config.yaml.
        g = p.add_argument_group("API overrides (optional; override llm_config.yaml)")
        g.add_argument("--provider", help="LLM provider: anthropic | openai | openai_compatible")
        g.add_argument("--base-url", help="Custom API base URL (e.g. https://api.example.com/v1)")
        g.add_argument("--api-key", help="API key for the LLM endpoint")
        g.add_argument(
            "--strong-model",
            help="Strong model for the main editing agent (e.g. claude-opus-4-8); "
                 "subagents fall back to --weak-model",
        )
        g.add_argument(
            "--weak-model",
            help="Cheap model for token-heavy subagents: diff-digest, log-analyst, "
                 "check-regen (e.g. claude-haiku-4-5)",
        )

    p_up = sub.add_parser("upgrade", help="Full multi-repo upgrade loop")
    p_up.add_argument("--repos-dir", default=".", help="Directory containing repos")
    p_up.add_argument("--config", help="[DEPRECATED] JSON config files are no longer supported; use --repos-dir")
    p_up.add_argument("--repos", nargs="+", help="Filter to specific repo names")
    p_up.add_argument("--base-ref", help="Base commit/branch/tag for downstream repos")
    p_up.add_argument("--base-llvm", help="LLVM commit hash for base-ref")
    p_up.add_argument("--target-hash", help="Target LLVM commit hash (default: main HEAD)")
    p_up.add_argument("--max-iterations", type=int, default=0, help="Max fix iterations (<=0 = unlimited)")
    _common(p_up)

    p_s = sub.add_parser("upgrade-single", help="Single-repo upgrade")
    p_s.add_argument("--repo", required=True, help="Repository name (e.g. 'circt')")
    p_s.add_argument("--repo-path", help="Path to the repository")
    p_s.add_argument("--repo-config", help="Path to a .syncbots.yml config")
    p_s.add_argument("--base-ref", help="Base commit/branch/tag of the downstream repo")
    p_s.add_argument("--base-llvm", help="LLVM commit hash for base-ref")
    p_s.add_argument("--target-llvm", help="Target LLVM commit hash")
    p_s.add_argument("--repos-dir", default=".", help="Directory containing repos")
    p_s.add_argument("--config", help="[DEPRECATED] JSON config files are no longer supported")
    p_s.add_argument("--max-iterations", type=int, default=0, help="Max fix iterations (<=0 = unlimited)")
    p_s.add_argument("--auto-branch", action="store_true", help="Auto-create a branch before upgrade")
    p_s.add_argument("--auto-pr", action="store_true", help="Commit, push, and open a PR after success")
    p_s.add_argument("--pr-base", default="", help="Base branch for the PR")
    p_s.add_argument("--branch-base", default="bump-llvm", help="Base branch for auto branch")
    p_s.add_argument("--branch-remote", default="origin", help="Remote for fetch/push")
    _common(p_s)

    p_b = sub.add_parser("bench", help="Run fixed benchmark cases (pass rate / iterations / tokens)")
    p_b.add_argument("--bench-config", required=True, help="Path to the benchmark YAML (top-level 'cases' list)")
    p_b.add_argument("--repos-dir", default=".", help="Directory containing repos")
    _common(p_b)

    p_a = sub.add_parser("analyze", help="Scan + prescan only (no code changes)")
    p_a.add_argument("--repos-dir", default=".", help="Directory containing repos")
    p_a.add_argument("--config", help="[DEPRECATED] JSON config files are no longer supported")
    p_a.add_argument("--repos", nargs="+", help="Filter to specific repo names")
    p_a.add_argument("--base-ref", help="Base commit/branch/tag for downstream repos")
    p_a.add_argument("--base-llvm", help="LLVM commit hash for base-ref")
    p_a.add_argument("--target-hash", help="Target LLVM commit hash")
    _common(p_a)

    sub.add_parser(
        "init",
        help="Interactive wizard: configure API + pick repo/path, then run",
    )

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    setup_logging(args.verbose)

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    handlers = {
        "upgrade": cmd_upgrade,
        "upgrade-single": cmd_upgrade_single,
        "analyze": cmd_analyze,
        "bench": cmd_bench,
        "init": cmd_init,
    }
    handler = handlers.get(args.command)
    if handler is None:
        parser.print_help()
        sys.exit(1)

    try:
        exit_code = handler(args)
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
        exit_code = 130
    except Exception as e:  # noqa: BLE001
        console.print(f"\n[red]Error: {e}[/red]")
        logging.getLogger(__name__).exception("Unhandled error")
        exit_code = 1

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
