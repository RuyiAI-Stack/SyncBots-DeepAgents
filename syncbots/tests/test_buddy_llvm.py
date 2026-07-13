"""Tests for buddy-mlir / custom LLVM fork configuration."""

from __future__ import annotations

import os
from unittest.mock import patch

from syncbots.cli import _guess_repo_path, _validate_repo_path
from syncbots.repo_config import load_builtin_config, yaml_to_repo_info
from syncbots.scan import resolve_target
from syncbots.state import RepoInfo
from syncbots.tools.llvm_tools import github_repo_from_url


def test_guess_repo_path_finds_home_buddy_mlir():
    if os.path.isdir(os.path.expanduser("~/buddy-mlir/.git")):
        assert _guess_repo_path("buddy-mlir") == os.path.abspath(os.path.expanduser("~/buddy-mlir"))


def test_validate_repo_path_suggests_home_when_default_missing():
    missing = "/tmp/syncbots-definitely-missing-repo"
    ok, msg = _validate_repo_path(missing, "buddy-mlir")
    assert not ok
    if os.path.isdir(os.path.expanduser("~/buddy-mlir")):
        assert "Did you mean" in msg


def test_buddy_mlir_builtin_config_has_ruyi_upstream():
    data = load_builtin_config("buddy-compiler/buddy-mlir")
    assert data is not None
    info = yaml_to_repo_info(data, repo_path="/tmp/buddy-mlir")
    assert info.llvm_upstream_github == "RuyiAI-Stack/llvm-project"
    assert info.llvm_upstream_branch == "riscv"
    assert info.llvm_upstream_url == "https://github.com/RuyiAI-Stack/llvm-project"


def test_github_repo_from_url():
    assert github_repo_from_url("https://github.com/RuyiAI-Stack/llvm-project") == "RuyiAI-Stack/llvm-project"
    assert github_repo_from_url("git@github.com:RuyiAI-Stack/llvm-project.git") == "RuyiAI-Stack/llvm-project"


@patch("syncbots.scan.query_llvm_main_head")
def test_resolve_target_uses_fork_branch(mock_head):
    mock_head.return_value = {"sha": "a" * 40}
    repo = RepoInfo(
        repo_name="buddy-compiler/buddy-mlir",
        local_path="/tmp/buddy-mlir",
        llvm_dep_type="submodule",
        llvm_dep_path="llvm",
        llvm_upstream_github="RuyiAI-Stack/llvm-project",
        llvm_upstream_branch="riscv",
    )
    target_hash, target_version = resolve_target(repo=repo)
    mock_head.assert_called_once_with("RuyiAI-Stack/llvm-project", "riscv")
    assert target_hash == "a" * 40
    assert target_version == ""
