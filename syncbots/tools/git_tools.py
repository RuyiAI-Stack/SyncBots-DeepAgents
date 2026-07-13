"""Git and subprocess helpers for the SyncBots pipeline.

These are plain functions used by the deterministic modules (scan, verify,
prescan). They are intentionally NOT exposed as deepagents tools -- the deep
agent gets its filesystem/shell access from the deepagents built-in tools and
the ``LocalShellBackend`` instead.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)


def run_cmd(
    cmd: str,
    cwd: str,
    timeout: int = 300,
    env: Optional[dict[str, str]] = None,
    stream: bool = False,
) -> tuple[int, str, str]:
    """Execute a shell command, returning (returncode, stdout, stderr).

    When *stream* is True, stdout/stderr are echoed to the logger in real time
    so long-running builds produce visible progress. Output is still captured.
    """
    run_env = os.environ.copy()
    if env:
        run_env.update(env)

    if not stream:
        try:
            result = subprocess.run(
                cmd,
                shell=True,
                executable="/bin/bash",
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
                env=run_env,
            )
            return result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            return -1, "", f"Command timed out after {timeout}s: {cmd}"
        except Exception as e:  # noqa: BLE001
            return -1, "", str(e)

    return _run_cmd_streaming(cmd, cwd, timeout, run_env)


def _run_cmd_streaming(
    cmd: str,
    cwd: str,
    timeout: int,
    run_env: dict[str, str],
) -> tuple[int, str, str]:
    """Run a command with real-time line-by-line output to the logger."""
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    try:
        proc = subprocess.Popen(
            cmd,
            shell=True,
            executable="/bin/bash",
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=run_env,
        )
    except Exception as e:  # noqa: BLE001
        return -1, "", str(e)

    def _reader(pipe, dest: list[str], prefix: str) -> None:
        try:
            for line in iter(pipe.readline, ""):
                dest.append(line)
                logger.info("    %s| %s", prefix, line.rstrip("\n"))
            pipe.close()
        except Exception:  # noqa: BLE001
            pass

    t_out = threading.Thread(target=_reader, args=(proc.stdout, stdout_lines, "out"), daemon=True)
    t_err = threading.Thread(target=_reader, args=(proc.stderr, stderr_lines, "err"), daemon=True)
    t_out.start()
    t_err.start()

    deadline = time.monotonic() + timeout
    while proc.poll() is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            proc.kill()
            t_out.join(timeout=5)
            t_err.join(timeout=5)
            return -1, "".join(stdout_lines), f"Command timed out after {timeout}s: {cmd}"
        t_out.join(timeout=min(remaining, 1.0))

    t_out.join(timeout=10)
    t_err.join(timeout=10)
    return proc.returncode, "".join(stdout_lines), "".join(stderr_lines)


def git_cmd(args: str, cwd: str, timeout: int = 300) -> tuple[int, str, str]:
    """Run a git command in the given directory."""
    return run_cmd(f"git {args}", cwd, timeout=timeout)


def git_get_submodule_hash(repo_path: str, submodule_path: str, ref: str = "HEAD") -> str:
    """Get the commit hash a submodule points to at a given ref ('' on failure)."""
    rc, out, _ = git_cmd(f"ls-tree {ref} {submodule_path}", repo_path)
    if rc != 0:
        return ""
    parts = out.strip().split()
    if len(parts) >= 3 and re.fullmatch(r"[0-9a-f]{40}", parts[2]):
        return parts[2]
    return ""


def git_submodule_update(repo_path: str, submodule_path: str, target_hash: str) -> str:
    """Update a git submodule to point to a specific commit hash."""
    sub_abs = os.path.join(repo_path, submodule_path)
    if not os.path.isdir(sub_abs):
        return f"ERROR: submodule path does not exist: {sub_abs}"

    rc, _, err = git_cmd("fetch origin", sub_abs, timeout=600)
    if rc != 0:
        rc2, _, err2 = git_cmd(f"fetch origin {target_hash}", sub_abs, timeout=600)
        if rc2 != 0:
            return f"ERROR: fetch failed: {err[:300]} / {err2[:300]}"

    rc, _, err = git_cmd(f"checkout {target_hash}", sub_abs)
    if rc != 0:
        return f"ERROR: checkout {target_hash[:12]} in submodule failed: {err[:500]}"

    rc, _, err = git_cmd(f"add {submodule_path}", repo_path)
    if rc != 0:
        return f"WARNING: git add submodule failed: {err[:300]}"
    return f"OK: submodule {submodule_path} updated to {target_hash[:12]}"


def git_working_diff(repo_path: str, max_chars: int = 4000) -> str:
    """Capture the current working-tree diff (vs HEAD) as a compact summary."""
    try:
        rc, stat, _ = git_cmd("diff HEAD --stat", repo_path, timeout=30)
        rc2, diff, _ = git_cmd("diff HEAD --no-color -U2", repo_path, timeout=60)
    except Exception as e:  # noqa: BLE001
        return f"(failed to capture diff: {e})"
    stat = (stat or "").strip()
    diff = (diff or "").strip()
    if len(diff) > max_chars:
        diff = diff[:max_chars] + f"\n... (truncated, {len(diff)} chars total)"
    return f"{stat}\n\n{diff}" if stat else "(no changes)"


def recover_repo_state(repo_path: str) -> None:
    """Best-effort recovery of git state (abort merges/rebases, clean conflicts)."""
    for abort_cmd in ("merge --abort", "rebase --abort", "cherry-pick --abort"):
        git_cmd(abort_cmd, repo_path)
    rc, out, _ = git_cmd("status --porcelain", repo_path)
    if rc != 0:
        return
    has_unmerged = any(len(line) >= 2 and "U" in line[:2] for line in out.splitlines())
    if has_unmerged:
        logger.warning("Detected unmerged index, auto-cleaning")
        git_cmd("reset --hard HEAD", repo_path)
        git_cmd("clean -fd", repo_path)
