"""LLVM-specific helpers: upstream version queries and local anchor resolution."""

from __future__ import annotations

import logging
import os
import re
from typing import Optional

import requests

from .git_tools import git_cmd

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
LLVM_REPO = "llvm/llvm-project"


def _github_headers() -> dict[str, str]:
    headers = {"Accept": "application/vnd.github.v3+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"token {token}"
    return headers


def github_repo_from_url(url: str) -> str:
    """Extract ``owner/repo`` from a GitHub HTTPS or SSH URL."""
    url = url.rstrip("/")
    if url.startswith("https://github.com/"):
        return url[len("https://github.com/") :]
    if url.startswith("git@github.com:"):
        return url[len("git@github.com:") :].removesuffix(".git")
    return ""


def query_branch_head(github_repo: str, branch: str) -> dict[str, str]:
    """Query the latest commit on *branch* of *github_repo*. Returns {'sha','date','message'} or {}."""
    if not github_repo or not branch:
        return {}
    try:
        resp = requests.get(
            f"{GITHUB_API}/repos/{github_repo}/commits/{branch}",
            headers=_github_headers(),
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        commit = data.get("commit", {})
        return {
            "sha": data.get("sha", ""),
            "date": commit.get("author", {}).get("date", ""),
            "message": commit.get("message", "").split("\n")[0],
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("query_branch_head(%s, %s) via API failed: %s", github_repo, branch, e)
        return _query_branch_head_git(github_repo, branch)


def _query_branch_head_git(github_repo: str, branch: str) -> dict[str, str]:
    """Fallback: resolve branch tip via ``git ls-remote`` (no API token required)."""
    rc, out, _ = git_cmd(
        f"ls-remote https://github.com/{github_repo}.git refs/heads/{branch}",
        ".",
        timeout=30,
    )
    if rc != 0:
        return {}
    line = out.strip().splitlines()[0] if out.strip() else ""
    sha = line.split()[0] if line else ""
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        return {}
    logger.info("Resolved %s@%s via git ls-remote: %s", github_repo, branch, sha[:12])
    return {"sha": sha, "date": "", "message": ""}


def query_llvm_main_head(github_repo: str = LLVM_REPO, branch: str = "main") -> dict[str, str]:
    """Query the latest commit on LLVM *branch*. Returns {'sha','date','message'} or {}."""
    return query_branch_head(github_repo, branch)


def query_llvm_latest_release() -> dict[str, str]:
    """Query the latest LLVM release. Returns {'tag','version','published'} or {}."""
    try:
        resp = requests.get(
            f"{GITHUB_API}/repos/{LLVM_REPO}/releases/latest",
            headers=_github_headers(),
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        tag = data.get("tag_name", "")
        return {
            "tag": tag,
            "version": tag.replace("llvmorg-", ""),
            "published": data.get("published_at", ""),
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("query_llvm_latest_release failed: %s", e)
        return {}


def resolve_local_llvm_hash(
    repo_path: str,
    llvm_dep_type: str,
    llvm_dep_path: str,
    ref: str = "HEAD",
) -> str:
    """Read the current LLVM commit hash from a local repository ('' on failure)."""
    if llvm_dep_type == "submodule":
        rc, out, _ = git_cmd(f"ls-tree {ref} {llvm_dep_path}", repo_path)
        if rc == 0:
            parts = out.strip().split()
            if len(parts) >= 3 and re.fullmatch(r"[0-9a-f]{40}", parts[2]):
                return parts[2]
        sub_abs = os.path.join(repo_path, llvm_dep_path)
        if os.path.isdir(sub_abs):
            rc, out, _ = git_cmd("rev-parse HEAD", sub_abs, timeout=10)
            if rc == 0 and re.fullmatch(r"[0-9a-f]{40}", out.strip()):
                return out.strip()
        return ""

    if llvm_dep_type == "hash_file":
        fpath = os.path.join(repo_path, llvm_dep_path)
        if not os.path.isfile(fpath):
            rc, out, _ = git_cmd(f"show {ref}:{llvm_dep_path}", repo_path)
            if rc != 0:
                return ""
            content = out
        else:
            try:
                with open(fpath, encoding="utf-8") as f:
                    content = f.read()
            except Exception:  # noqa: BLE001
                return ""
        m = re.search(r"\b[0-9a-f]{40}\b", content)
        if m:
            return m.group(0)
        m = re.search(r"\b[0-9a-f]{12,39}\b", content)
        return m.group(0) if m else ""

    return ""


def update_hash_file(repo_path: str, llvm_dep_path: str, new_hash: str) -> str:
    """Replace the LLVM hash in a hash_file anchor with *new_hash*."""
    fpath = os.path.join(repo_path, llvm_dep_path)
    if not os.path.isfile(fpath):
        return f"ERROR: file not found: {fpath}"
    try:
        with open(fpath, encoding="utf-8") as f:
            content = f.read()
        m = re.search(r"\b[0-9a-f]{40}\b", content) or re.search(r"\b[0-9a-f]{12,39}\b", content)
        new_content = content[: m.start()] + new_hash + content[m.end():] if m else new_hash + "\n"
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(new_content)
        return f"OK: updated hash in {llvm_dep_path} to {new_hash[:12]}"
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {e}"


def ensure_llvm_commits(
    llvm_repo_path: str,
    *hashes: str,
    upstream_github: str = "",
    upstream_branch: str = "",
) -> None:
    """Fetch missing commits into a local LLVM checkout (e.g. buddy-mlir/llvm submodule)."""
    if not llvm_repo_path or not os.path.isdir(llvm_repo_path):
        return
    for h in hashes:
        if not h:
            continue
        rc, _, _ = git_cmd(f"cat-file -t {h}", llvm_repo_path, timeout=10)
        if rc == 0:
            continue
        logger.info("  Fetching LLVM commit %s into %s", h[:12], llvm_repo_path)
        git_cmd(f"fetch origin {h}", llvm_repo_path, timeout=600)
        rc, _, _ = git_cmd(f"cat-file -t {h}", llvm_repo_path, timeout=10)
        if rc == 0:
            continue
        if upstream_branch:
            git_cmd(f"fetch origin {upstream_branch}", llvm_repo_path, timeout=600)
        if upstream_github and upstream_github != "llvm/llvm-project":
            git_cmd(f"fetch https://github.com/{upstream_github}.git {h}", llvm_repo_path, timeout=600)


def plan_upgrade_segments(
    llvm_repo_path: Optional[str],
    from_hash: str,
    to_hash: str,
    max_span: int,
) -> list[str]:
    """Split a large LLVM upgrade into staged segment targets.

    When the first-parent commit span from *from_hash* to *to_hash* exceeds
    *max_span*, return a list of intermediate commit hashes (every *max_span*
    commits, ending with *to_hash*) so the upgrade can be driven as several
    small, well-attributed steps instead of one giant leap.

    Returns ``[to_hash]`` (single segment) when the span is small, *max_span*
    is disabled (<= 0), or the local LLVM checkout cannot answer.
    """
    single = [to_hash]
    if max_span <= 0 or not from_hash or not to_hash or from_hash == to_hash:
        return single
    if not llvm_repo_path or not os.path.isdir(llvm_repo_path):
        return single

    rc, out, _ = git_cmd(
        f"rev-list --count --first-parent {from_hash}..{to_hash}", llvm_repo_path, timeout=60,
    )
    if rc != 0:
        return single
    try:
        count = int(out.strip())
    except ValueError:
        return single
    if count <= max_span:
        return single

    # Oldest-first, first-parent history keeps midpoints on the main line.
    rc, out, _ = git_cmd(
        f"rev-list --reverse --first-parent {from_hash}..{to_hash}", llvm_repo_path, timeout=120,
    )
    if rc != 0:
        return single
    commits = [c for c in out.split() if re.fullmatch(r"[0-9a-f]{40}", c)]
    if not commits:
        return single

    segments = [commits[i] for i in range(max_span - 1, len(commits), max_span)]
    if not segments or segments[-1] != commits[-1]:
        segments.append(commits[-1])
    logger.info(
        "[segments] span of %d commits split into %d segment(s) (max %d each)",
        count, len(segments), max_span,
    )
    return segments


def classify_upgrade(from_hash: str, to_hash: str, llvm_repo_path: Optional[str] = None) -> str:
    """Classify the upgrade magnitude between two LLVM commits."""
    if from_hash == to_hash:
        return "sync"
    if not llvm_repo_path or not os.path.isdir(llvm_repo_path):
        return "unknown"
    rc, out, _ = git_cmd(f"rev-list --count {from_hash}..{to_hash}", llvm_repo_path, timeout=30)
    if rc != 0:
        return "unknown"
    try:
        count = int(out.strip())
    except ValueError:
        return "unknown"
    if count == 0:
        return "sync"
    if count < 50:
        return "patch"
    if count < 500:
        return "minor"
    return "major"
