"""Loop engineering controller -- the outer driver of the upgrade.

This is the heart of SyncBots' loop engineering. It periodically executes the
deep agent and, after each run, verifies the repo. **Unit tests passing is the
sole success-exit condition.** On failure it diagnoses the error, guards against
repeating an identical failure, injects a targeted fix strategy, and runs the
agent again.

Responsibility boundary:
  - This controller owns iteration count, verification, success/stop decisions,
    diagnosis, de-duplication, and strategy selection.
  - The deep agent owns autonomous code reading/editing within a single run.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from typing import Optional

from ..agent.builder import build_upgrade_agent
from ..agent.trace import invoke_and_trace, make_run_log_dir, write_text
from ..llm import LLMConfigSet
from ..prescan import format_prescan_report, grep_repo
from ..scan import apply_target_llvm, find_llvm_repo, scan_repo
from ..state import AgentIterationReport, FixAttempt, RepoInfo, UpgradeResult, VerifyResult
from ..tools.git_tools import git_working_diff
from ..tools.llvm_tools import plan_upgrade_segments
from .verifier import verify

logger = logging.getLogger(__name__)

DEFAULT_MAX_ITERATIONS = 0  # <= 0 means unlimited
MAX_IDENTICAL_FAILURES = 3  # stop if the same failure recurs this many times
DEFAULT_SEGMENT_MAX_SPAN = 1000  # split upgrades spanning more LLVM commits; <=0 disables

# error_type -> the split fix skill the agent should consult (progressive
# disclosure: it only reads the one SKILL.md matching the current failure).
SKILL_FOR_ERROR_TYPE = {
    "compile_error": "compile-error-fix",
    "linker_error": "linker-error-fix",
    "tablegen_error": "tablegen-error-fix",
    "cmake_error": "cmake-error-fix",
    "test_failure": "test-failure-fix",
    "unknown": "unknown-error-triage",
}


def _fingerprint(result: VerifyResult) -> str:
    """Stable fingerprint of a failure for de-duplication.

    Normalizes volatile parts (line numbers, hex addresses, temp paths) so two
    runs that fail "the same way" collapse to one fingerprint.
    """
    text = f"{result.phase}|{result.error_type}|{result.error_summary}"
    text = re.sub(r":\d+:\d+:", ":N:N:", text)
    text = re.sub(r"0x[0-9a-f]+", "0xADDR", text)
    text = re.sub(r"/tmp/\S+", "/tmp/T", text)
    text = re.sub(r"\b\d{3,}\b", "N", text)
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]


def _build_initial_task(
    repo: RepoInfo,
    prescan_report: str,
    seg_index: int = 0,
    seg_total: int = 1,
    final_target: str = "",
) -> str:
    """Build the first-iteration task message for the agent."""
    parts = [
        f"# LLVM upgrade task for {repo.repo_name}",
        "",
        f"Repository path: {repo.local_path}",
        f"LLVM dependency: {repo.llvm_dep_type} at {repo.llvm_dep_path}",
        f"Current LLVM: {repo.current_llvm_hash[:12] or '?'}",
        f"Target LLVM:  {repo.target_llvm_hash[:12] or '?'}",
        f"Upgrade type: {repo.upgrade_type}",
    ]
    if seg_total > 1:
        parts.append(
            f"Staged upgrade: segment {seg_index + 1}/{seg_total} "
            f"(final target: {final_target[:12] or '?'}). Adapt the code ONLY "
            "to the current segment's target; later segments handle the rest."
        )
    if repo.llvm_upstream_github:
        upstream = repo.llvm_upstream_github
        if repo.llvm_upstream_branch:
            upstream = f"{upstream} ({repo.llvm_upstream_branch})"
        parts.append(f"LLVM upstream fork: {upstream}")
    parts += [
        "",
        "The version anchor has ALREADY been updated to the target by the loop. "
        "Your job is to adapt the downstream source code to the new LLVM/MLIR API.",
    ]
    if prescan_report:
        parts += ["", prescan_report]
    else:
        parts += [
            "",
            "Prescan found no known old-API usages. The upgrade may need no source "
            "changes; apply anything obvious, then let the loop build and verify.",
        ]
    parts += [
        "",
        "If you need to understand what changed in LLVM, call the `diff-digest` "
        "subagent rather than reading the raw diff. Locate code with the "
        "three-layer protocol (glob -> grep -> read_file, see the `code-search` "
        "skill). When done editing, stop.",
    ]
    return "\n".join(parts)


def _build_fix_task(repo: RepoInfo, result: VerifyResult, iteration: int, history: list[FixAttempt]) -> str:
    """Build a fix-iteration task message with diagnosis (strategy lives in skill)."""
    skill = SKILL_FOR_ERROR_TYPE.get(result.error_type, "unknown-error-triage")
    parts = [
        f"# Fix iteration {iteration} for {repo.repo_name}",
        f"The previous attempt FAILED during the **{result.phase}** phase.",
        f"Error type: `{result.error_type}`",
        "",
        f"Consult the `{skill}` skill for the `{result.error_type}` "
        "playbook before editing. Locate code with the three-layer protocol "
        "(glob -> grep -> read_file, see the `code-search` skill).",
        "",
        "## Error log (condensed)",
        "```",
        result.error_summary[:4000],
        "```",
    ]
    if result.log_path:
        parts += [
            "",
            f"The FULL {result.phase} log is saved at: `{result.log_path}`",
            "If the condensed log above is insufficient, read that file (or pass "
            "its path to the `log-analyst` subagent). Do NOT guess other log paths.",
        ]
    if history:
        prev = ", ".join(f"#{h.iteration}:{h.error_type}" for h in history[-4:])
        parts += [
            "",
            f"## Previous attempts: {prev}",
            "Do NOT repeat a fix that already failed. If the same error persists, "
            "change approach and address the root cause.",
        ]
    parts += [
        "",
        "If the log is confusing, call `log-analyst`. For FileCheck failures, call "
        "`check-regen`. When done editing, stop.",
    ]
    return "\n".join(parts)


def run_upgrade(
    repo: RepoInfo,
    target_llvm_hash: str = "",
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    llm_config: Optional[LLMConfigSet] = None,
    override_model: Optional[str] = None,
    enable_memory: bool = True,
    do_scan: bool = True,
    show_agent: bool = False,
    agent_log_dir: Optional[str] = None,
    output_dir: Optional[str] = None,
    segment_max_span: int = DEFAULT_SEGMENT_MAX_SPAN,
) -> UpgradeResult:
    """Drive the full loop-engineered upgrade for one repository.

    Large upgrades (first-parent span > *segment_max_span* LLVM commits) are
    automatically staged: the range is split into intermediate segment targets
    and each segment runs its own prescan + fix loop, so diffs stay small and
    failures stay attributable. *max_iterations* applies per segment.
    """
    if do_scan:
        scan_repo(repo, target_hash=target_llvm_hash)
    elif target_llvm_hash:
        repo.target_llvm_hash = target_llvm_hash

    if repo.status == "skipped":
        return UpgradeResult(
            repo_name=repo.repo_name, status="skipped",
            error_message="Could not resolve LLVM anchor.",
        )

    run_start = time.time()

    def _finish(result: UpgradeResult) -> UpgradeResult:
        """Stamp duration and archive verified learnings into the memory store."""
        result.duration_sec = time.time() - run_start
        if enable_memory:
            try:
                from ..memory import MemoryStore, record_run_learnings

                added = record_run_learnings(MemoryStore(), repo, result)
                if added:
                    logger.info("[memory] archived %d learning(s)", added)
            except Exception as e:  # noqa: BLE001
                logger.warning("[memory] failed to archive learnings: %s", e)
        return result

    # ── Plan segments: split a huge LLVM span into staged targets ──
    final_target = repo.target_llvm_hash
    segments = plan_upgrade_segments(
        find_llvm_repo([repo]), repo.current_llvm_hash, final_target, segment_max_span,
    )
    n_seg = len(segments)
    if n_seg > 1:
        logger.info(
            "[loop] %s staged upgrade: %d segments -> %s",
            repo.repo_name, n_seg, ", ".join(s[:12] for s in segments),
        )

    log_dir = make_run_log_dir(
        repo.local_path, agent_log_dir,
        repo_name=repo.repo_name, output_root=output_dir,
    )
    logs_dir = os.path.join(log_dir, "logs")
    logger.info("[agent] run logs: %s", log_dir)

    agent = build_upgrade_agent(
        repo_path=repo.local_path,
        llm_config=llm_config,
        override_model=override_model,
        enable_memory=enable_memory,
    )

    repo.status = "upgrading"
    history: list[FixAttempt] = []
    agent_reports: list[AgentIterationReport] = []
    segments_completed = 0

    def _result(status: str, error_message: str = "") -> UpgradeResult:
        return UpgradeResult(
            repo_name=repo.repo_name, status=status,
            iterations_used=len(agent_reports),
            target_llvm_hash=final_target, history=history,
            error_message=error_message,
            agent_reports=agent_reports, log_dir=log_dir,
            segments_total=n_seg, segments_completed=segments_completed,
        )

    for seg_idx, seg_target in enumerate(segments):
        repo.target_llvm_hash = seg_target
        ok, bump_msg = apply_target_llvm(repo)
        if not ok:
            repo.status = "skipped"
            return _finish(_result("skipped", f"Failed to apply target LLVM: {bump_msg}"))
        logger.info("  %s", bump_msg)

        # ── Per-segment deterministic prescan ──
        prescan_report = _build_prescan(repo, llm_config, override_model)
        prescan_name = "prescan.md" if n_seg == 1 else f"prescan-seg{seg_idx}.md"
        write_text(os.path.join(log_dir, prescan_name), prescan_report or "(no prescan patterns)\n")

        if n_seg > 1:
            logger.info(
                "[loop] %s segment %d/%d -> %s",
                repo.repo_name, seg_idx + 1, n_seg, seg_target[:12],
            )

        task = _build_initial_task(
            repo, prescan_report,
            seg_index=seg_idx, seg_total=n_seg, final_target=final_target,
        )
        seen_fingerprints: dict[str, int] = {}
        seg_iteration = 0
        seg_passed = False

        while max_iterations <= 0 or seg_iteration < max_iterations:
            step = len(agent_reports)  # global iteration id (log/transcript labels)
            logger.info("[loop] %s iteration %d", repo.repo_name, step)
            report = invoke_and_trace(
                agent, task, iteration=step, show_live=show_agent, log_dir=log_dir,
            )
            agent_reports.append(report)

            result = verify(
                repo, clean_first=(step == 0),
                log_dir=logs_dir, label=f"iter{step}",
            )
            if result.success:
                seg_passed = True
                segments_completed += 1
                repo.current_llvm_hash = seg_target  # anchor for the next segment
                logger.info(
                    "[loop] %s segment %d/%d PASSED at iteration %d",
                    repo.repo_name, seg_idx + 1, n_seg, step,
                )
                break

            # ── Diagnose + de-duplicate ──
            fp = _fingerprint(result)
            seen_fingerprints[fp] = seen_fingerprints.get(fp, 0) + 1
            history.append(FixAttempt(
                iteration=step, phase=result.phase, error_type=result.error_type,
                error_summary=result.error_summary[:3000],
                diff=git_working_diff(repo.local_path), fingerprint=fp,
            ))

            if seen_fingerprints[fp] >= MAX_IDENTICAL_FAILURES:
                repo.status = "build_fail" if result.phase == "build" else "test_fail"
                logger.warning(
                    "[loop] %s aborting: identical failure repeated %d times (fp=%s)",
                    repo.repo_name, seen_fingerprints[fp], fp,
                )
                _write_run_summary(log_dir, agent_reports, repo, repo.status,
                                   segments_completed, n_seg)
                return _finish(_result(
                    repo.status,
                    f"Identical {result.error_type} failure repeated; root cause not resolved.",
                ))

            seg_iteration += 1
            task = _build_fix_task(repo, result, len(agent_reports), history)

        if not seg_passed:
            repo.status = "upgrade_fail"
            _write_run_summary(log_dir, agent_reports, repo, "upgrade_fail",
                               segments_completed, n_seg)
            seg_note = f" (segment {seg_idx + 1}/{n_seg})" if n_seg > 1 else ""
            return _finish(_result(
                "upgrade_fail",
                f"Reached max_iterations ({max_iterations}) without passing tests{seg_note}.",
            ))

    repo.status = "pass"
    repo.target_llvm_hash = final_target
    logger.info("[loop] %s PASSED (all %d segment(s))", repo.repo_name, n_seg)
    _write_run_summary(log_dir, agent_reports, repo, "pass", segments_completed, n_seg)
    return _finish(_result("pass"))


def _build_prescan(
    repo: RepoInfo,
    llm_config: Optional[LLMConfigSet] = None,
    override_model: Optional[str] = None,
) -> str:
    """Build the grep-verified prescan report.

    Patterns are derived at runtime by the diff-digest subagent (no static KB),
    then deterministically grep-verified against the downstream repo.
    """
    from ..digest import extract_api_change_patterns

    patterns = extract_api_change_patterns(repo, llm_config, override_model)
    if not patterns:
        return ""
    hits = grep_repo(repo, patterns)
    report = format_prescan_report(hits)
    if report:
        logger.info("[prescan] %s: %d patterns matched", repo.repo_name, len(hits))
    return report


def _write_run_summary(
    log_dir: str,
    reports: list[AgentIterationReport],
    repo: RepoInfo,
    status: str,
    segments_completed: int = 0,
    segments_total: int = 1,
) -> None:
    """Write a top-level summary of all agent iterations (incl. token usage)."""
    total_in = sum(r.input_tokens for r in reports)
    total_out = sum(r.output_tokens for r in reports)
    total = sum(r.total_tokens for r in reports)
    lines = [
        "# SyncBots agent run summary",
        "",
        f"- repo: {repo.repo_name}",
        f"- status: {status}",
        f"- llvm: {repo.current_llvm_hash[:12]} -> {repo.target_llvm_hash[:12]}",
        f"- tokens: in={total_in} out={total_out} total={total}",
    ]
    if segments_total > 1:
        lines.append(f"- segments: {segments_completed}/{segments_total} completed")
    lines += [
        "",
        "## Iterations",
        "",
    ]
    for r in reports:
        lines += [
            f"### Iteration {r.iteration}",
            "",
            f"tokens: in={r.input_tokens} out={r.output_tokens} total={r.total_tokens}",
            "",
            r.summary,
            "",
            f"Full transcript: `iteration-{r.iteration}.md`",
            "",
        ]
    write_text(os.path.join(log_dir, "summary.md"), "\n".join(lines))
