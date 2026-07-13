"""Unit tests for prepare_repo (restore -> fetch latest -> update submodules)."""

from __future__ import annotations

import syncbots.scan as scan
from syncbots.state import RepoInfo


def _repo(tmp_path, **kw):
    defaults = dict(
        repo_name="test/repo", local_path=str(tmp_path),
        llvm_dep_type="submodule", llvm_dep_path="llvm",
    )
    defaults.update(kw)
    return RepoInfo(**defaults)


def _record_git(calls, responses=None):
    responses = responses or {}

    def fake_git(args, cwd, timeout=300):
        calls.append(args)
        for prefix, resp in responses.items():
            if args.startswith(prefix):
                return resp
        return 0, "", ""

    return fake_git


def test_prepare_restores_then_syncs_baseline_then_updates_submodules(tmp_path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(scan, "git_cmd", _record_git(
        calls, {"symbolic-ref refs/remotes/origin/HEAD": (0, "refs/remotes/origin/main\n", "")}
    ))
    monkeypatch.setattr(scan, "recover_repo_state", lambda p: calls.append("<recover>"))
    monkeypatch.setattr(scan, "_sync_llvm_dep", lambda r: calls.append("<sync_llvm>"))

    assert scan.prepare_repo(_repo(tmp_path)) is True

    def idx(prefix):
        return next(i for i, c in enumerate(calls) if c.startswith(prefix))

    # restore -> fetch -> checkout baseline -> force-reset to origin -> submodules
    assert idx("<recover>") < idx("reset --hard HEAD")
    assert idx("reset --hard HEAD") < idx("fetch origin --prune")
    assert idx("fetch origin --prune") < idx("checkout main")
    assert idx("checkout main") < idx("reset --hard origin/main")
    assert idx("reset --hard origin/main") < idx("submodule update --init")
    assert idx("submodule update --init") < idx("<sync_llvm>")
    # Never recurse: the nested vendored trees (riscv-gnu-toolchain -> gcc/qemu/
    # a second LLVM) must never be re-pulled.
    assert not any("--recursive" in c for c in calls)
    # No fast-forward pull anymore; the baseline is a hard reset to origin.
    assert not any(c.startswith("pull ") for c in calls)


def test_prepare_checks_out_base_ref_and_force_resets(tmp_path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(scan, "git_cmd", _record_git(calls))
    monkeypatch.setattr(scan, "recover_repo_state", lambda p: None)
    monkeypatch.setattr(scan, "_sync_llvm_dep", lambda r: None)

    assert scan.prepare_repo(_repo(tmp_path, base_ref="v1.0")) is True
    assert any(c.startswith("checkout v1.0") for c in calls)
    assert any(c.startswith("reset --hard origin/v1.0") for c in calls)
    assert not any(c.startswith("pull ") for c in calls)


def test_prepare_fails_when_baseline_checkout_fails(tmp_path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(scan, "git_cmd", _record_git(
        calls,
        {
            "checkout v1.0": (1, "", "unknown revision"),
            "checkout -B v1.0": (1, "", "unknown revision"),
        },
    ))
    monkeypatch.setattr(scan, "recover_repo_state", lambda p: None)
    monkeypatch.setattr(scan, "_sync_llvm_dep", lambda r: None)

    assert scan.prepare_repo(_repo(tmp_path, base_ref="v1.0")) is False


def test_prepare_survives_fetch_failure(tmp_path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(scan, "git_cmd", _record_git(
        calls,
        {
            "fetch origin --prune": (1, "", "network down"),
            "symbolic-ref refs/remotes/origin/HEAD": (0, "refs/remotes/origin/main\n", ""),
        },
    ))
    monkeypatch.setattr(scan, "recover_repo_state", lambda p: None)
    monkeypatch.setattr(scan, "_sync_llvm_dep", lambda r: None)

    assert scan.prepare_repo(_repo(tmp_path)) is True
    # Offline: we skip fetching the branch but still reach submodule update.
    assert not any(c.startswith("fetch origin main") for c in calls)
    assert any(c.startswith("submodule update --init") for c in calls)


def test_prepare_cleans_up_stale_upgrade_branches(tmp_path, monkeypatch):
    calls: list[str] = []
    branch_listing = (
        0,
        "main\nupgrade/bump-llvm-fast_forward-20260101-000000\n"
        "syncbots/bump-20260101-000000\nfeature/keep-me\n",
        "",
    )
    monkeypatch.setattr(scan, "git_cmd", _record_git(
        calls,
        {
            "symbolic-ref refs/remotes/origin/HEAD": (0, "refs/remotes/origin/main\n", ""),
            "branch --format": branch_listing,
        },
    ))
    monkeypatch.setattr(scan, "recover_repo_state", lambda p: None)
    monkeypatch.setattr(scan, "_sync_llvm_dep", lambda r: None)

    assert scan.prepare_repo(_repo(tmp_path)) is True
    deleted = [c for c in calls if c.startswith("branch -D")]
    assert "branch -D upgrade/bump-llvm-fast_forward-20260101-000000" in deleted
    assert "branch -D syncbots/bump-20260101-000000" in deleted
    # The baseline branch and unrelated branches must be preserved.
    assert not any(c.startswith("branch -D main") for c in calls)
    assert not any("feature/keep-me" in c for c in calls)
