"""Unit tests for the curated memory store (dedup, pruning, rendering, archiving)."""

from __future__ import annotations

from syncbots.memory import MemoryEntry, MemoryStore, record_run_learnings
from syncbots.state import AgentIterationReport, FixAttempt, RepoInfo, UpgradeResult


def _store(tmp_path):
    return MemoryStore(memory_dir=str(tmp_path / "mem"))


def _repo():
    return RepoInfo(
        repo_name="test/repo", local_path="/tmp/x",
        llvm_dep_type="hash_file", llvm_dep_path="llvm-hash.txt",
        current_llvm_hash="a" * 40, target_llvm_hash="b" * 40,
    )


def test_add_and_load_roundtrip(tmp_path):
    store = _store(tmp_path)
    assert store.add(MemoryEntry(lesson="Renamed getODSOperands -> getOperands", error_type="compile_error"))
    entries = store.load()
    assert len(entries) == 1
    assert entries[0].error_type == "compile_error"


def test_dedup_by_normalized_lesson(tmp_path):
    store = _store(tmp_path)
    assert store.add(MemoryEntry(lesson="Fixed  foo   bar", error_type="compile_error"))
    # Same lesson modulo whitespace/case -> refreshed, not appended.
    assert store.add(MemoryEntry(lesson="fixed foo bar", error_type="compile_error", verified=True)) is False
    entries = store.load()
    assert len(entries) == 1
    assert entries[0].verified is True  # verified upgraded on dedup


def test_prune_caps_per_type_and_prefers_verified(tmp_path):
    store = _store(tmp_path)
    for i in range(12):
        store.add(MemoryEntry(lesson=f"lesson variant {i} zzz", error_type="compile_error",
                              verified=(i == 11)))
    pruned = store.prune(max_per_type=3)
    assert len(pruned) == 3
    assert pruned[0].verified  # verified entry ranked first


def test_prune_drops_stale_unverified(tmp_path):
    store = _store(tmp_path)
    store.add(MemoryEntry(lesson="old unverified qqq", error_type="unknown",
                          updated_at="2000-01-01T00:00:00Z"))
    store.add(MemoryEntry(lesson="old verified qqq2", error_type="unknown", verified=True,
                          updated_at="2000-01-01T00:00:00Z"))
    pruned = store.prune()
    lessons = [e.lesson for e in pruned]
    assert "old verified qqq2" in lessons
    assert "old unverified qqq" not in lessons


def test_render_markdown_groups_by_type(tmp_path):
    store = _store(tmp_path)
    store.add(MemoryEntry(lesson="cmake target renamed", error_type="cmake_error", verified=True))
    store.add(MemoryEntry(lesson="check lines regenerated", error_type="test_failure"))
    md = store.render_markdown()
    assert "## cmake_error" in md
    assert "## test_failure" in md
    assert "[unverified]" in md  # the test_failure entry


def test_write_rendered_empty_store_returns_empty(tmp_path):
    assert _store(tmp_path).write_rendered() == ""


def test_record_learnings_on_pass_stores_verified(tmp_path):
    store = _store(tmp_path)
    result = UpgradeResult(
        repo_name="test/repo", status="pass", iterations_used=2,
        history=[FixAttempt(iteration=0, phase="build", error_type="compile_error",
                            error_summary="boom")],
        agent_reports=[
            AgentIterationReport(iteration=0, summary="initial edits"),
            AgentIterationReport(iteration=1, summary="Replaced old API with new signature"),
        ],
    )
    added = record_run_learnings(store, _repo(), result)
    assert added == 1
    entry = store.load()[0]
    assert entry.verified is True
    assert entry.error_type == "compile_error"
    assert "Replaced old API" in entry.lesson


def test_record_learnings_on_failure_stores_negative(tmp_path):
    store = _store(tmp_path)
    result = UpgradeResult(
        repo_name="test/repo", status="build_fail", iterations_used=3,
        history=[FixAttempt(iteration=i, phase="build", error_type="linker_error",
                            error_summary="undefined ref") for i in range(3)],
        agent_reports=[AgentIterationReport(iteration=i, summary=f"attempt {i}") for i in range(3)],
    )
    added = record_run_learnings(store, _repo(), result)
    assert added == 1
    entry = store.load()[0]
    assert entry.verified is False
    assert "UNRESOLVED linker_error" in entry.lesson
