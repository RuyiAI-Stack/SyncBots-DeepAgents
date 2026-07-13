"""Unit tests for the loop controller's exit conditions and de-duplication."""

from __future__ import annotations

import syncbots.loop.controller as ctrl
from syncbots.state import RepoInfo, VerifyResult


def _repo(tmp_path):
    return RepoInfo(
        repo_name="test/repo", local_path=str(tmp_path),
        llvm_dep_type="hash_file", llvm_dep_path="llvm-hash.txt",
        current_llvm_hash="a" * 40, target_llvm_hash="b" * 40, status="pending",
    )


def _fail(phase="build", etype="compile_error", summary="x.cpp:1:1: error: boom"):
    return VerifyResult(build_ok=(phase != "build"), tests_passed=False,
                        phase=phase, error_type=etype, error_summary=summary)


def _ok():
    return VerifyResult(build_ok=True, tests_passed=True, phase="done")


def test_fingerprint_ignores_line_numbers():
    a = _fail(summary="a.cpp:10:5: error: X")
    b = _fail(summary="a.cpp:88:2: error: X")
    assert ctrl._fingerprint(a) == ctrl._fingerprint(b)


def test_fingerprint_differs_by_error_type():
    a = _fail(etype="compile_error")
    b = _fail(etype="linker_error")
    assert ctrl._fingerprint(a) != ctrl._fingerprint(b)


def test_success_exits_when_tests_pass(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setattr(ctrl, "build_upgrade_agent", lambda **kw: object())
    monkeypatch.setattr(ctrl, "apply_target_llvm", lambda repo: (True, "OK"))
    monkeypatch.setattr(ctrl, "invoke_and_trace", lambda *a, **k: __import__("syncbots.state", fromlist=["AgentIterationReport"]).AgentIterationReport(iteration=k.get("iteration", 0), summary="ok"))
    monkeypatch.setattr(ctrl, "_build_prescan", lambda repo, *a, **k: "")
    monkeypatch.setattr(ctrl, "git_working_diff", lambda p: "")
    monkeypatch.setattr(ctrl, "verify", lambda repo, clean_first=False, **kw: _ok())

    result = ctrl.run_upgrade(repo, max_iterations=5, do_scan=False, enable_memory=False,
                              output_dir=str(tmp_path / "out"))
    assert result.status == "pass"
    assert result.iterations_used == 1


def test_identical_failure_aborts(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setattr(ctrl, "build_upgrade_agent", lambda **kw: object())
    monkeypatch.setattr(ctrl, "apply_target_llvm", lambda repo: (True, "OK"))
    monkeypatch.setattr(ctrl, "invoke_and_trace", lambda *a, **k: __import__("syncbots.state", fromlist=["AgentIterationReport"]).AgentIterationReport(iteration=k.get("iteration", 0), summary="ok"))
    monkeypatch.setattr(ctrl, "_build_prescan", lambda repo, *a, **k: "")
    monkeypatch.setattr(ctrl, "git_working_diff", lambda p: "")
    # Always returns the same failure -> should abort after MAX_IDENTICAL_FAILURES
    monkeypatch.setattr(ctrl, "verify", lambda repo, clean_first=False, **kw: _fail())

    result = ctrl.run_upgrade(repo, max_iterations=0, do_scan=False, enable_memory=False,
                              output_dir=str(tmp_path / "out"))
    assert result.status == "build_fail"
    assert result.iterations_used == ctrl.MAX_IDENTICAL_FAILURES


def test_max_iterations_exhausted(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setattr(ctrl, "build_upgrade_agent", lambda **kw: object())
    monkeypatch.setattr(ctrl, "apply_target_llvm", lambda repo: (True, "OK"))
    monkeypatch.setattr(ctrl, "invoke_and_trace", lambda *a, **k: __import__("syncbots.state", fromlist=["AgentIterationReport"]).AgentIterationReport(iteration=k.get("iteration", 0), summary="ok"))
    monkeypatch.setattr(ctrl, "_build_prescan", lambda repo, *a, **k: "")
    monkeypatch.setattr(ctrl, "git_working_diff", lambda p: "")
    # Distinct failures each time so de-dup never trips; exhaust max_iterations.
    seq = iter(range(100))
    monkeypatch.setattr(
        ctrl, "verify",
        lambda repo, clean_first=False, **kw: _fail(summary=f"err variant {next(seq)} qqq"),
    )

    result = ctrl.run_upgrade(repo, max_iterations=2, do_scan=False, enable_memory=False,
                              output_dir=str(tmp_path / "out"))
    assert result.status == "upgrade_fail"
    assert result.iterations_used == 2


def test_skipped_repo_short_circuits(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    repo.status = "skipped"
    monkeypatch.setattr(ctrl, "scan_repo", lambda repo, target_hash="": repo)
    result = ctrl.run_upgrade(repo, do_scan=True, enable_memory=False)
    assert result.status == "skipped"
