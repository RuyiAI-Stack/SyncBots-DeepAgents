"""Unit tests for staged (segmented) upgrades: planning and loop orchestration."""

from __future__ import annotations

import syncbots.loop.controller as ctrl
import syncbots.tools.llvm_tools as lt
from syncbots.state import AgentIterationReport, RepoInfo, VerifyResult

H_FROM = "a" * 40
H_MID = "c" * 40
H_TO = "b" * 40


# ── plan_upgrade_segments ────────────────────────────────────────────────────

def _fake_git(count: int, commits: list[str]):
    def fake(args, cwd, timeout=300):
        if args.startswith("rev-list --count"):
            return 0, f"{count}\n", ""
        if args.startswith("rev-list --reverse"):
            return 0, "\n".join(commits) + "\n", ""
        return 1, "", "unexpected"
    return fake


def test_small_span_single_segment(tmp_path, monkeypatch):
    monkeypatch.setattr(lt, "git_cmd", _fake_git(10, []))
    assert lt.plan_upgrade_segments(str(tmp_path), H_FROM, H_TO, 100) == [H_TO]


def test_large_span_is_segmented(tmp_path, monkeypatch):
    commits = [f"{i:040x}" for i in range(1, 251)]  # 250 commits
    monkeypatch.setattr(lt, "git_cmd", _fake_git(250, commits))
    segs = lt.plan_upgrade_segments(str(tmp_path), H_FROM, H_TO, 100)
    assert segs == [commits[99], commits[199], commits[249]]


def test_disabled_or_degenerate_returns_single(tmp_path, monkeypatch):
    monkeypatch.setattr(lt, "git_cmd", _fake_git(9999, []))
    assert lt.plan_upgrade_segments(str(tmp_path), H_FROM, H_TO, 0) == [H_TO]
    assert lt.plan_upgrade_segments(None, H_FROM, H_TO, 100) == [H_TO]
    assert lt.plan_upgrade_segments(str(tmp_path), H_TO, H_TO, 100) == [H_TO]


def test_git_failure_returns_single(tmp_path, monkeypatch):
    monkeypatch.setattr(lt, "git_cmd", lambda *a, **k: (1, "", "boom"))
    assert lt.plan_upgrade_segments(str(tmp_path), H_FROM, H_TO, 100) == [H_TO]


# ── segmented run_upgrade ────────────────────────────────────────────────────

def _repo(tmp_path):
    return RepoInfo(
        repo_name="test/repo", local_path=str(tmp_path),
        llvm_dep_type="hash_file", llvm_dep_path="llvm-hash.txt",
        current_llvm_hash=H_FROM, target_llvm_hash=H_TO, status="pending",
    )


def _patch_common(monkeypatch, applied_targets):
    monkeypatch.setattr(ctrl, "build_upgrade_agent", lambda **kw: object())
    monkeypatch.setattr(
        ctrl, "apply_target_llvm",
        lambda repo: applied_targets.append(repo.target_llvm_hash) or (True, "OK"),
    )
    monkeypatch.setattr(
        ctrl, "invoke_and_trace",
        lambda *a, **k: AgentIterationReport(iteration=k.get("iteration", 0), summary="ok"),
    )
    monkeypatch.setattr(ctrl, "_build_prescan", lambda repo, *a, **k: "")
    monkeypatch.setattr(ctrl, "git_working_diff", lambda p: "")


def test_all_segments_pass(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    applied: list[str] = []
    _patch_common(monkeypatch, applied)
    monkeypatch.setattr(ctrl, "plan_upgrade_segments", lambda *a, **k: [H_MID, H_TO])
    monkeypatch.setattr(
        ctrl, "verify",
        lambda repo, clean_first=False, **kw: VerifyResult(build_ok=True, tests_passed=True, phase="done"),
    )

    result = ctrl.run_upgrade(repo, max_iterations=5, do_scan=False, enable_memory=False,
                              output_dir=str(tmp_path / "out"))
    assert result.status == "pass"
    assert result.segments_total == 2
    assert result.segments_completed == 2
    assert result.iterations_used == 2  # one agent run per segment
    assert applied == [H_MID, H_TO]
    assert result.target_llvm_hash == H_TO
    assert repo.current_llvm_hash == H_TO  # anchor advanced through segments


def test_failing_segment_stops_staging(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    applied: list[str] = []
    _patch_common(monkeypatch, applied)
    monkeypatch.setattr(ctrl, "plan_upgrade_segments", lambda *a, **k: [H_MID, H_TO])
    # First segment always fails identically -> abort inside segment 1.
    monkeypatch.setattr(
        ctrl, "verify",
        lambda repo, clean_first=False, **kw: VerifyResult(
            build_ok=False, tests_passed=False, phase="build",
            error_type="compile_error", error_summary="x.cpp:1:1: error: boom",
        ),
    )

    result = ctrl.run_upgrade(repo, max_iterations=0, do_scan=False, enable_memory=False,
                              output_dir=str(tmp_path / "out"))
    assert result.status == "build_fail"
    assert result.segments_total == 2
    assert result.segments_completed == 0
    assert applied == [H_MID]  # second segment never attempted


def test_single_segment_behaves_as_before(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    applied: list[str] = []
    _patch_common(monkeypatch, applied)
    monkeypatch.setattr(ctrl, "plan_upgrade_segments", lambda *a, **k: [H_TO])
    monkeypatch.setattr(
        ctrl, "verify",
        lambda repo, clean_first=False, **kw: VerifyResult(build_ok=True, tests_passed=True, phase="done"),
    )

    result = ctrl.run_upgrade(repo, max_iterations=5, do_scan=False, enable_memory=False,
                              output_dir=str(tmp_path / "out"))
    assert result.status == "pass"
    assert result.segments_total == 1
    assert result.iterations_used == 1
