"""Deterministic scan: resolve LLVM anchors and the upgrade target.

This replaces the original ScannerAgent. It mutates a :class:`RepoInfo` in
place: checking out the base ref, syncing the LLVM dependency, reading the
current LLVM hash, resolving the target hash, and classifying the upgrade.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from .state import RepoInfo
from .tools.git_tools import git_cmd, recover_repo_state
from .tools.llvm_tools import (
    classify_upgrade,
    ensure_llvm_commits,
    query_llvm_latest_release,
    query_llvm_main_head,
    resolve_local_llvm_hash,
)

logger = logging.getLogger(__name__)


def find_llvm_repo(repos: list[RepoInfo]) -> Optional[str]:
    """Find a local llvm-project checkout for commit comparison."""
    # Prefer nested LLVM submodules (e.g. buddy-mlir/llvm) before global checkouts.
    candidates: list[str] = []
    for repo in repos:
        if repo.llvm_dep_type == "submodule":
            candidates.append(os.path.join(repo.local_path, repo.llvm_dep_path))
    candidates.append(os.path.expanduser("~/llvm-project"))
    for path in candidates:
        path = os.path.realpath(path)
        if os.path.isdir(os.path.join(path, ".git")) or os.path.isdir(os.path.join(path, "mlir")):
            return path
    return None


def resolve_target(
    target_hash: str = "",
    target_version: str = "",
    repo: Optional[RepoInfo] = None,
) -> tuple[str, str]:
    """Resolve the target LLVM hash/version, querying upstream if not provided."""
    if target_hash:
        return target_hash, target_version

    if repo and repo.llvm_upstream_github and repo.llvm_upstream_branch:
        logger.info(
            "No target hash specified, querying %s@%s...",
            repo.llvm_upstream_github,
            repo.llvm_upstream_branch,
        )
        head = query_llvm_main_head(repo.llvm_upstream_github, repo.llvm_upstream_branch)
        if head.get("sha"):
            target_hash = head["sha"]
            logger.info(
                "Using %s/%s HEAD: %s",
                repo.llvm_upstream_github,
                repo.llvm_upstream_branch,
                target_hash[:12],
            )
        return target_hash, target_version

    logger.info("No target hash specified, querying LLVM upstream...")
    main = query_llvm_main_head()
    if main.get("sha"):
        target_hash = main["sha"]
        logger.info("Using LLVM main HEAD: %s", target_hash[:12])
    rel = query_llvm_latest_release()
    if rel.get("version"):
        target_version = rel["version"]
    return target_hash, target_version


# Local branch prefixes the pipeline uses for its throwaway upgrade branches.
# These are safe to delete on every fresh run (we always re-create them).
_UPGRADE_BRANCH_PREFIXES = ("upgrade/bump-llvm-", "upgrade/bump-", "syncbots/bump-")


def _detect_default_branch(path: str) -> str:
    """Return origin's default branch name (e.g. ``main``), falling back to main."""
    rc, out, _ = git_cmd("symbolic-ref refs/remotes/origin/HEAD", path)
    if rc == 0 and out.strip():
        return out.strip().rsplit("/", 1)[-1]
    # origin/HEAD may not be set locally -- try to (re)discover it once.
    git_cmd("remote set-head origin -a", path, timeout=60)
    rc, out, _ = git_cmd("symbolic-ref refs/remotes/origin/HEAD", path)
    if rc == 0 and out.strip():
        return out.strip().rsplit("/", 1)[-1]
    return "main"


def _cleanup_stale_upgrade_branches(path: str, keep: str) -> None:
    """Delete leftover throwaway upgrade branches, keeping the *keep* branch."""
    rc, out, _ = git_cmd("branch --format=%(refname:short)", path)
    if rc != 0:
        return
    for br in (line.strip() for line in out.splitlines()):
        if not br or br == keep:
            continue
        if br.startswith(_UPGRADE_BRANCH_PREFIXES):
            rc, _, err = git_cmd(f"branch -D {br}", path)
            if rc == 0:
                logger.info("  Deleted stale upgrade branch %s", br)
            else:
                logger.warning("  Failed to delete branch %s: %s", br, err.strip()[:150])


def prepare_repo(repo: RepoInfo) -> bool:
    """Restore the repo to a pristine baseline before any upgrade work.

    Every run starts from a clean, up-to-date default branch so the agent builds
    on the latest ``origin`` state (including changes the user pushed there),
    never on a leftover upgrade branch from a previous run. Phases:
      1. restore: abort in-flight merges/rebases, drop local edits/untracked;
      2. sync baseline: fetch, check out the default branch (or configured
         ``base_ref``) and ``reset --hard`` it to ``origin/<branch>``, then prune
         stale upgrade branches;
      3. components: sync + update git submodules (non-recursively) and the
         LLVM dependency.

    ``reset --hard origin/<branch>`` discards any un-pushed local commits on that
    branch by design -- the baseline is always the remote.

    Returns ``False`` only when the baseline branch cannot be checked out; every
    other step is best-effort.
    """
    path = repo.local_path

    # ── 1. Restore to a clean working tree ──
    logger.info("  Restoring %s to a clean state", repo.repo_name)
    recover_repo_state(path)
    git_cmd("reset --hard HEAD", path)
    if repo.llvm_dep_type == "submodule":
        git_cmd(f"checkout HEAD -- {repo.llvm_dep_path}", path)
    git_cmd("clean -fd", path)

    # ── 2. Sync to the baseline branch and force-align it to origin ──
    logger.info("  Fetching latest refs for %s", repo.repo_name)
    rc, _, err = git_cmd("fetch origin --prune", path, timeout=600)
    offline = rc != 0
    if offline:
        logger.warning("  fetch origin failed (continuing offline): %s", err.strip()[:200])

    branch = repo.base_ref or _detect_default_branch(path)
    logger.info("  Switching %s to baseline branch '%s'", repo.repo_name, branch)
    if not offline:
        git_cmd(f"fetch origin {branch}", path, timeout=600)

    rc, _, _ = git_cmd(f"checkout {branch}", path)
    if rc != 0:
        # No local branch yet -- create it tracking origin.
        rc, _, err = git_cmd(f"checkout -B {branch} origin/{branch}", path)
        if rc != 0:
            logger.error("  Failed to checkout baseline branch '%s' for %s: %s",
                         branch, repo.repo_name, err.strip())
            return False

    # Force the baseline to the remote so we build on the latest pushed state.
    rc, _, err = git_cmd(f"reset --hard origin/{branch}", path)
    if rc != 0:
        logger.warning("  reset --hard origin/%s failed (keeping local HEAD): %s",
                       branch, err.strip()[:200])
    git_cmd("clean -fd", path)

    _cleanup_stale_upgrade_branches(path, keep=branch)

    # ── 3. Update top-level submodules WITHOUT recursing ──
    # We deliberately never pass ``--recursive`` here. Downstream repos vendor
    # enormous nested submodule trees -- e.g. buddy-mlir's
    # ``thirdparty/riscv-gnu-toolchain`` pulls in gcc/glibc/gdb/qemu/newlib and a
    # *second* full LLVM (tens of GB). Those are already checked out and their
    # pointers almost never move, so a non-recursive top-level update is a cheap
    # no-op when nothing changed and never triggers a fresh multi-GB clone.
    # The LLVM dependency itself is synced precisely (specific commit, in place)
    # by ``_sync_llvm_dep`` below -- no re-clone of the 13G llvm tree.
    logger.info("  Updating top-level submodules for %s (non-recursive)", repo.repo_name)
    git_cmd("submodule sync", path, timeout=120)
    rc, _, err = git_cmd("submodule update --init", path, timeout=1800)
    if rc != 0:
        logger.warning("  submodule update --init failed: %s", err.strip()[:300])

    _sync_llvm_dep(repo)
    return True


def _sync_llvm_dep(repo: RepoInfo) -> None:
    """Sync the LLVM dependency to the version recorded for the repo."""
    llvm_hash = repo.base_llvm
    if repo.llvm_dep_type == "submodule":
        sub_abs = os.path.join(repo.local_path, repo.llvm_dep_path)
        logger.info("  Syncing LLVM submodule at %s", repo.llvm_dep_path)
        git_cmd("submodule sync", repo.local_path, timeout=60)
        rc, _, _ = git_cmd(f"submodule update --init {repo.llvm_dep_path}", repo.local_path, timeout=600)
        if rc != 0 and os.path.isdir(sub_abs):
            logger.warning("  submodule update failed, trying fetch in submodule...")
            git_cmd("fetch origin", sub_abs, timeout=600)
            git_cmd(f"submodule update --init {repo.llvm_dep_path}", repo.local_path, timeout=600)
        if llvm_hash and os.path.isdir(sub_abs):
            logger.info("  Overriding LLVM submodule to base_llvm: %s", llvm_hash[:12])
            if repo.llvm_upstream_url:
                git_cmd(f"remote set-url origin {repo.llvm_upstream_url}", sub_abs, timeout=30)
            elif repo.llvm_upstream_github:
                git_cmd(
                    f"remote set-url origin https://github.com/{repo.llvm_upstream_github}.git",
                    sub_abs,
                    timeout=30,
                )
            git_cmd(f"fetch origin {llvm_hash}", sub_abs, timeout=600)
            rc, _, err = git_cmd(f"checkout {llvm_hash}", sub_abs)
            if rc != 0:
                logger.warning("  checkout base_llvm in submodule failed: %s", err.strip())
    elif repo.llvm_dep_type == "hash_file" and llvm_hash:
        hash_file = os.path.join(repo.local_path, repo.llvm_dep_path)
        logger.info("  Writing base_llvm %s to %s", llvm_hash[:12], repo.llvm_dep_path)
        try:
            with open(hash_file, "w", encoding="utf-8") as f:
                f.write(llvm_hash + "\n")
        except OSError as e:
            logger.warning("  Failed to write hash file: %s", e)


def scan_repo(repo: RepoInfo, target_hash: str = "", target_version: str = "") -> RepoInfo:
    """Resolve anchors and target for one repo, mutating and returning it."""
    if not os.path.isdir(repo.local_path):
        logger.error("  Repository path does not exist: %s", repo.local_path)
        repo.status = "skipped"
        return repo

    target_hash, target_version = resolve_target(target_hash, target_version, repo)
    llvm_repo_path = find_llvm_repo([repo])

    # Restore -> fetch latest -> update submodules, BEFORE any upgrade work.
    if not prepare_repo(repo):
        repo.status = "skipped"
        return repo

    if repo.base_llvm:
        repo.current_llvm_hash = repo.base_llvm
        logger.info("  Using explicit base LLVM: %s", repo.base_llvm[:12])
    else:
        repo.current_llvm_hash = resolve_local_llvm_hash(
            repo.local_path, repo.llvm_dep_type, repo.llvm_dep_path
        )

    repo.target_llvm_hash = target_hash
    repo.current_llvm_version = target_version  # informational

    if llvm_repo_path and repo.current_llvm_hash and target_hash:
        ensure_llvm_commits(
            llvm_repo_path,
            repo.current_llvm_hash,
            target_hash,
            upstream_github=repo.llvm_upstream_github,
            upstream_branch=repo.llvm_upstream_branch,
        )

    if repo.current_llvm_hash and target_hash:
        repo.upgrade_type = classify_upgrade(repo.current_llvm_hash, target_hash, llvm_repo_path)
    else:
        repo.upgrade_type = "unknown"

    if not repo.current_llvm_hash:
        repo.status = "skipped"
        logger.warning("Could not read LLVM anchor for %s, skipping", repo.repo_name)
    else:
        repo.status = "pending"

    logger.info(
        "  %s: %s -> %s (%s) [%s]",
        repo.repo_name,
        repo.current_llvm_hash[:12] if repo.current_llvm_hash else "?",
        target_hash[:12] if target_hash else "?",
        repo.upgrade_type,
        repo.status,
    )
    return repo


def apply_target_llvm(repo: RepoInfo) -> tuple[bool, str]:
    """Checkout the LLVM dependency to ``repo.target_llvm_hash``.

    For submodules this checks out the commit inside the nested repo and stages
    the pointer update in the parent. For hash_file repos it rewrites the hash
    file. ``repo.current_llvm_hash`` is left unchanged (pre-upgrade anchor for
    diff/prescan).
    """
    target = (repo.target_llvm_hash or "").strip()
    if not target:
        return False, "no target LLVM hash"

    current = (repo.current_llvm_hash or "").strip()
    if current == target:
        logger.info("  LLVM anchor already at target %s", target[:12])
        return True, "OK: already at target"

    logger.info("  Bumping LLVM anchor %s -> %s", current[:12] or "?", target[:12])

    if repo.llvm_dep_type == "submodule":
        sub_abs = os.path.join(repo.local_path, repo.llvm_dep_path)
        if not os.path.isdir(sub_abs):
            return False, f"submodule path does not exist: {sub_abs}"
        if repo.llvm_upstream_url:
            git_cmd(f"remote set-url origin {repo.llvm_upstream_url}", sub_abs, timeout=30)
        elif repo.llvm_upstream_github:
            git_cmd(
                f"remote set-url origin https://github.com/{repo.llvm_upstream_github}.git",
                sub_abs,
                timeout=30,
            )
        ensure_llvm_commits(
            sub_abs,
            target,
            upstream_github=repo.llvm_upstream_github,
            upstream_branch=repo.llvm_upstream_branch,
        )
        rc, _, err = git_cmd(f"checkout {target}", sub_abs)
        if rc != 0:
            return False, f"checkout {target[:12]} in submodule failed: {err.strip()}"
        rc, _, err = git_cmd(f"add {repo.llvm_dep_path}", repo.local_path)
        if rc != 0:
            return False, f"git add submodule failed: {err.strip()}"
        return True, f"OK: submodule {repo.llvm_dep_path} -> {target[:12]}"

    if repo.llvm_dep_type == "hash_file":
        from .tools.llvm_tools import update_hash_file

        msg = update_hash_file(repo.local_path, repo.llvm_dep_path, target)
        return msg.startswith("OK:"), msg

    return False, f"unsupported llvm_dep_type: {repo.llvm_dep_type}"
