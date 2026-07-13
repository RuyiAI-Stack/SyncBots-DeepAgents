"""Tests for deterministic LLVM anchor bump before upgrade."""

from __future__ import annotations

from syncbots.scan import apply_target_llvm
from syncbots.state import RepoInfo


def test_apply_target_skips_when_already_at_target(tmp_path, monkeypatch):
    repo = RepoInfo(
        repo_name="test/repo",
        local_path=str(tmp_path),
        llvm_dep_type="submodule",
        llvm_dep_path="llvm",
        current_llvm_hash="a" * 40,
        target_llvm_hash="a" * 40,
    )
    called = []
    monkeypatch.setattr("syncbots.scan.git_cmd", lambda *a, **k: called.append(a) or (0, "", ""))
    ok, msg = apply_target_llvm(repo)
    assert ok
    assert "already" in msg
    assert not called


def test_apply_target_checkout_submodule(tmp_path, monkeypatch):
    llvm_dir = tmp_path / "llvm"
    llvm_dir.mkdir()
    repo = RepoInfo(
        repo_name="buddy-compiler/buddy-mlir",
        local_path=str(tmp_path),
        llvm_dep_type="submodule",
        llvm_dep_path="llvm",
        current_llvm_hash="0" * 40,
        target_llvm_hash="f" * 40,
        llvm_upstream_github="RuyiAI-Stack/llvm-project",
        llvm_upstream_branch="riscv",
    )

    cmds: list[str] = []

    def fake_git(args, cwd, timeout=300):
        cmds.append(f"{args} @ {cwd}")
        return 0, "", ""

    monkeypatch.setattr("syncbots.scan.git_cmd", fake_git)
    monkeypatch.setattr("syncbots.scan.ensure_llvm_commits", lambda *a, **k: None)

    ok, msg = apply_target_llvm(repo)
    assert ok, msg
    assert any("checkout" in c and "f" * 40 in c for c in cmds)
    assert any("add llvm" in c for c in cmds)
    assert any("remote set-url" in c and "RuyiAI-Stack" in c for c in cmds)
