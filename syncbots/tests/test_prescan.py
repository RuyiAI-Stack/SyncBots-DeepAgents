"""Unit tests for prescan grep verification and config loading."""

from __future__ import annotations

import shutil

import pytest

from syncbots.prescan import format_prescan_report, grep_repo, is_specific_pattern
from syncbots.state import RepoInfo


def test_is_specific_pattern():
    assert is_specific_pattern("getOrInsertDeclaration")
    assert not is_specific_pattern("type")  # generic
    assert not is_specific_pattern("abc")  # too short


def test_format_prescan_report_empty():
    assert format_prescan_report({}) == ""


def test_format_prescan_report_renders_locations():
    report = format_prescan_report({"FooBarBaz": ["src/a.cpp", "src/b.cpp"]})
    assert "FooBarBaz" in report
    assert "src/a.cpp" in report
    assert "2 patterns" not in report  # only 1 pattern
    assert "1 patterns" in report


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_grep_repo_finds_pattern(tmp_path):
    (tmp_path / "src").mkdir()
    f = tmp_path / "src" / "x.cpp"
    f.write_text("void f() { getOrInsertDeclaration(); }\n")
    repo = RepoInfo(
        repo_name="t/t", local_path=str(tmp_path),
        llvm_dep_type="submodule", llvm_dep_path="llvm",
    )
    hits = grep_repo(repo, ["getOrInsertDeclaration", "type"])
    assert "getOrInsertDeclaration" in hits
    assert "type" not in hits  # filtered as generic


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_grep_repo_excludes_llvm_submodule(tmp_path):
    (tmp_path / "llvm" / "inc").mkdir(parents=True)
    (tmp_path / "llvm" / "inc" / "y.cpp").write_text("getOrInsertDeclaration();\n")
    (tmp_path / "own.cpp").write_text("getOrInsertDeclaration();\n")
    repo = RepoInfo(
        repo_name="t/t", local_path=str(tmp_path),
        llvm_dep_type="submodule", llvm_dep_path="llvm",
    )
    hits = grep_repo(repo, ["getOrInsertDeclaration"])
    locs = hits.get("getOrInsertDeclaration", [])
    assert any("own.cpp" in loc for loc in locs)
    assert not any("llvm/inc" in loc for loc in locs)
