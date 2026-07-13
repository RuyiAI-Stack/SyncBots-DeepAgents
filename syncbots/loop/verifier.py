"""Deterministic build/test verifier -- the loop's success determiner.

This module owns the **sole success-exit condition**: unit tests passing.
It runs the repo's build commands, then (only if the build succeeds) the test
commands, parsing output into a :class:`VerifyResult`. Error logs are condensed
to the most useful lines so the loop controller can hand a compact summary to
the deep agent.

Optimizations vs the original BuilderAgent:
  - first-error-stop: build/test stop at the first failing command;
  - optional incremental build via ``build_commands`` ordering (the repo config
    decides whether a clean is needed -- the controller only cleans on iter 0).
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Optional

from ..state import RepoInfo, VerifyResult
from ..tools.git_tools import run_cmd

logger = logging.getLogger(__name__)

MAX_ERROR_LOG_CHARS = 8000


def _write_log(log_dir: Optional[str], name: str, content: str) -> str:
    """Write *content* to ``<log_dir>/<name>`` and return the path ('' on skip/fail)."""
    if not log_dir or not content:
        return ""
    try:
        os.makedirs(log_dir, exist_ok=True)
        path = os.path.join(log_dir, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path
    except OSError as e:  # noqa: BLE001
        logger.warning("  failed to write log %s: %s", name, e)
        return ""


# ── Log condensing ───────────────────────────────────────────────────────────

def _strip_filecheck_dump(log: str) -> str:
    """Remove FileCheck -dump-input blocks (<<<<<<...>>>>>>) -- noise for the LLM."""
    if "<<<<<<" not in log:
        return log
    out_lines: list[str] = []
    in_dump = False
    for line in log.splitlines():
        stripped = line.strip().lstrip("| ").strip()
        if re.search(r"<<<<<<\s*$", stripped):
            in_dump = True
            continue
        if in_dump:
            if re.search(r">>>>>>\s*$", stripped):
                in_dump = False
            continue
        if re.match(r"\s*check:\d+'\d+", stripped):
            continue
        out_lines.append(line)
    return "\n".join(out_lines)


def _summarize_filecheck_errors(log: str) -> str:
    """Build a concise header summarizing FileCheck 'expected string not found'."""
    error_pattern = re.compile(
        r"([^\s:]+):(\d+):\d+:\s*error:\s*CHECK[^:]*:\s*expected string not found"
    )
    check_pattern = re.compile(r"//\s*CHECK[^:]*:\s*(.*)")
    errors: list[dict] = []
    lines = log.splitlines()
    for i, line in enumerate(lines):
        m = error_pattern.search(line)
        if not m:
            continue
        check_text = ""
        for j in range(i + 1, min(i + 3, len(lines))):
            cm = check_pattern.search(lines[j])
            if cm:
                check_text = cm.group(1).strip()[:80]
                break
        errors.append({"file": m.group(1).rsplit("/", 1)[-1], "line": m.group(2), "check": check_text})
    if not errors:
        return ""
    unique = {re.sub(r"%\[\[[A-Z_0-9]+:\.\*\]\]", "%[[...]]", e["check"]) for e in errors}
    files = {e["file"] for e in errors}
    parts = [
        f"## FileCheck Error Summary ({len(errors)} errors in {len(files)} test file(s))",
        f"   Unique patterns: {len(unique)} -- fix ALL errors in one pass!\n",
    ]
    for idx, e in enumerate(errors, 1):
        parts.append(f"  {idx}. {e['file']}:{e['line']} -- {e['check']}")
    parts.append("")
    return "\n".join(parts)


def _extract_error_lines(log: str, max_chars: int = MAX_ERROR_LOG_CHARS) -> str:
    """Extract the most useful error snippets from build/test output."""
    log = _strip_filecheck_dump(log)
    filecheck_summary = _summarize_filecheck_errors(log)
    if len(log) <= max_chars:
        return (filecheck_summary + "\n" + log) if filecheck_summary else log

    lines = log.splitlines()
    compiler = [i for i, L in enumerate(lines) if re.search(r":\d+:\d+: (?:error|fatal error):", L)]
    failed = [i for i, L in enumerate(lines) if re.search(r"^FAILED:", L)]
    linker = [i for i, L in enumerate(lines)
              if re.search(r"undefined reference to|multiple definition of|ld returned", L)]
    testfail = [i for i, L in enumerate(lines) if re.search(
        r"expected string not found|CHECK-LABEL:.*not found|FAIL:|UNEXPECTED FAILURE|"
        r"FileCheck error:|error:\s*CHECK.*not found|Failed Tests \(\d+\):", L, re.IGNORECASE)]
    general = [i for i, L in enumerate(lines) if re.search(
        r"error:|Error |FAILED|fatal error|undefined reference|no member named|"
        r"was not declared|cannot convert|no matching function|has no member|"
        r"incomplete type|redefinition of", L, re.IGNORECASE)]

    snippets: list[str] = []
    seen: set[int] = set()

    def _add(indices: list[int], before: int, after: int, max_group: int) -> None:
        added = 0
        for idx in indices:
            if added >= max_group:
                break
            start, end = max(0, idx - before), min(len(lines), idx + after + 1)
            if all(j in seen for j in range(start, end)):
                continue
            for j in range(start, end):
                seen.add(j)
            snippets.append("\n".join(lines[start:end]))
            added += 1

    _add(compiler, 2, 4, 15)
    _add(failed, 1, 2, 5)
    _add(linker, 1, 2, 5)
    _add(testfail, 3, 5, 10)
    if not snippets:
        _add(general, 3, 8, 15)

    tail = "\n".join(lines[-25:])
    if snippets:
        body = "\n...\n".join(snippets) + "\n\n--- output tail ---\n" + tail
        result = (filecheck_summary + "\n" + body) if filecheck_summary else body
        return result[:max_chars]
    return log[-max_chars:]


# ── Error classification ─────────────────────────────────────────────────────

def classify_error(error_summary: str) -> str:
    """Classify a build/test error for routing the fix strategy."""
    if not error_summary:
        return "unknown"
    if re.search(
        r"\.cpp\.inc:\d+:\d+: (?:error|fatal error):|\.h\.inc:\d+:\d+: (?:error|fatal error):|"
        r"error:.*TableGen|include/.*\.inc.*error:", error_summary):
        return "tablegen_error"
    if re.search(
        r"CMake Error|Could not find.*module|Unknown CMake command|cmake.*error|"
        r"No rule to make target", error_summary, re.IGNORECASE):
        return "cmake_error"
    if re.search(
        r"undefined reference to|multiple definition of|ld returned \d+ exit|"
        r"cannot find -l|undefined symbol", error_summary):
        return "linker_error"
    if re.search(
        r"FAIL:|UNEXPECTED FAILURE|FileCheck error:|CHECK-LABEL:.*not found|"
        r"error:.*expected string not found|TEST.*FAILED|tests? failed",
        error_summary, re.IGNORECASE):
        return "test_failure"
    if re.search(
        r":\d+:\d+: (?:error|fatal error):|no member named|no matching function|"
        r"was not declared|cannot convert|incomplete type|undeclared identifier|"
        r"has no member", error_summary):
        return "compile_error"
    return "unknown"


# ── Command execution ────────────────────────────────────────────────────────

def _run_sequence(repo: RepoInfo, commands: list[str], timeout: int, extra_env: dict[str, str] | None = None) -> tuple[bool, str, str]:
    """Run commands sequentially; stop at first failure.

    Returns ``(ok, error_summary, full_log)`` -- ``full_log`` is the complete raw
    output of every command run (for writing to disk), while ``error_summary`` is
    the condensed snippet handed to the agent.
    """
    env = {**repo.env, **(extra_env or {})}
    all_logs: list[str] = []
    for cmd in commands:
        cmd = cmd.replace("{repo_path}", repo.local_path)
        logger.info("  exec: %s", cmd[:120])
        start = time.time()
        rc, out, err = run_cmd(cmd, repo.local_path, timeout=timeout, env=env, stream=True)
        elapsed = time.time() - start
        all_logs.append(f"$ {cmd}\n{out}\n{err}\n--- exit {rc} ({elapsed:.1f}s) ---\n")
        if rc != 0:
            full = "\n".join(all_logs)
            return False, _extract_error_lines(full), full
    return True, "", "\n".join(all_logs)


def run_clean(repo: RepoInfo) -> None:
    """Run clean commands (best-effort; failures are logged and ignored)."""
    if not repo.clean_commands:
        return
    env = repo.env
    for cmd in repo.clean_commands:
        cmd = cmd.replace("{repo_path}", repo.local_path)
        logger.info("  clean: %s", cmd)
        rc, _, err = run_cmd(cmd, repo.local_path, timeout=300, env=env)
        if rc != 0:
            logger.warning("  clean returned %d (ignored): %s", rc, err[:200])


def verify(
    repo: RepoInfo,
    clean_first: bool = False,
    log_dir: Optional[str] = None,
    label: str = "verify",
) -> VerifyResult:
    """Build then test the repo. Tests passing is the sole success signal.

    Full build/test output is written to ``<log_dir>/<label>_build.log`` and
    ``<label>_test.log`` so the agent (and the user) can inspect complete logs.
    """
    if clean_first:
        run_clean(repo)

    start = time.time()
    if repo.build_commands:
        build_ok, build_err, build_log = _run_sequence(repo, repo.build_commands, repo.timeout_build)
    else:
        build_ok, build_err, build_log = True, "", ""

    build_log_path = _write_log(log_dir, f"{label}_build.log", build_log)

    if not build_ok:
        return VerifyResult(
            build_ok=False, tests_passed=False, phase="build",
            error_type=classify_error(build_err), error_summary=build_err,
            log_path=build_log_path, duration_sec=time.time() - start,
        )

    if repo.test_commands:
        test_env = {"FILECHECK_OPTS": "-dump-input=never"}
        tests_ok, test_err, test_log = _run_sequence(repo, repo.test_commands, repo.timeout_test, test_env)
    else:
        tests_ok, test_err, test_log = True, "", ""

    test_log_path = _write_log(log_dir, f"{label}_test.log", test_log)

    if not tests_ok:
        return VerifyResult(
            build_ok=True, tests_passed=False, phase="test",
            error_type=classify_error(test_err), error_summary=test_err,
            log_path=test_log_path, duration_sec=time.time() - start,
        )

    return VerifyResult(
        build_ok=True, tests_passed=True, phase="done",
        log_path=test_log_path or build_log_path,
        duration_sec=time.time() - start,
    )
