"""YAML-based repository configuration loader.

Each downstream repository can be described by a ``.syncbots.yml`` (or the
legacy ``.llvm-upgrader.yml``) file placed either in the repo root
(user-provided, highest priority) or in the built-in ``repos/`` directory.

The ``${repo_path}`` placeholder in any string value is replaced with the
repository's absolute path at load time.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

import yaml

from .state import RepoInfo

logger = logging.getLogger(__name__)

REPO_CONFIG_FILENAMES = (".syncbots.yml",)
_BUILTIN_DIR = Path(__file__).parent / "repos"

KNOWN_LLVM_ANCHORS: list[dict] = [
    {"repo_name": "llvm/circt", "llvm_dep_type": "submodule", "llvm_dep_path": "llvm"},
    {"repo_name": "iree-org/iree", "llvm_dep_type": "submodule", "llvm_dep_path": "third_party/llvm-project"},
    {"repo_name": "triton-lang/triton", "llvm_dep_type": "hash_file", "llvm_dep_path": "cmake/llvm-hash.txt"},
    {"repo_name": "openxla/stablehlo", "llvm_dep_type": "hash_file", "llvm_dep_path": "build_tools/llvm_version.txt"},
    {"repo_name": "llvm/torch-mlir", "llvm_dep_type": "submodule", "llvm_dep_path": "externals/llvm-project"},
    {"repo_name": "buddy-compiler/buddy-mlir", "llvm_dep_type": "submodule", "llvm_dep_path": "llvm",
     "llvm_upstream_github": "RuyiAI-Stack/llvm-project", "llvm_upstream_branch": "riscv",
     "llvm_upstream_url": "https://github.com/RuyiAI-Stack/llvm-project"},
]

DIR_TO_REPO: dict[str, str] = {
    "circt": "llvm/circt",
    "iree": "iree-org/iree",
    "triton": "triton-lang/triton",
    "stablehlo": "openxla/stablehlo",
    "torch-mlir": "llvm/torch-mlir",
    "buddy-mlir": "buddy-compiler/buddy-mlir",
}


def _substitute(value: Any, repo_path: str) -> Any:
    if isinstance(value, str):
        return value.replace("${repo_path}", repo_path).replace("{repo_path}", repo_path)
    if isinstance(value, list):
        return [_substitute(v, repo_path) for v in value]
    if isinstance(value, dict):
        return {k: _substitute(v, repo_path) for k, v in value.items()}
    return value


def load_repo_yaml(path: str | Path) -> dict[str, Any]:
    """Read and return the raw dict from a YAML config file."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"Repo config not found: {path}")
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"Invalid repo config (expected mapping): {path}")
    return data


def _github_repo_from_url(url: str) -> str:
    """Extract ``owner/repo`` from a GitHub HTTPS URL."""
    url = url.rstrip("/")
    if url.startswith("https://github.com/"):
        return url[len("https://github.com/") :]
    if url.startswith("git@github.com:"):
        return url[len("git@github.com:") :].removesuffix(".git")
    return ""


def yaml_to_repo_info(
    data: dict[str, Any],
    repo_path: str = "",
    base_dir: str = "",
) -> RepoInfo:
    """Convert a YAML dict into a :class:`RepoInfo`, applying path substitution."""
    if not repo_path:
        raw_local = data.get("local_path", "")
        if raw_local:
            if not os.path.isabs(raw_local):
                raw_local = os.path.join(base_dir or os.getcwd(), raw_local)
            repo_path = raw_local
    repo_path = os.path.abspath(repo_path) if repo_path else ""
    data = _substitute(data, repo_path)

    llvm = data.get("llvm", {})
    build = data.get("build", {})
    test = data.get("test", {})

    upstream = llvm.get("upstream", {})
    upstream_github = llvm.get("upstream_github", "") or upstream.get("github", "")
    upstream_branch = llvm.get("upstream_branch", "") or upstream.get("branch", "")
    upstream_url = llvm.get("upstream_url", "") or upstream.get("url", "")
    if not upstream_github and upstream_url:
        upstream_github = _github_repo_from_url(upstream_url)

    return RepoInfo(
        repo_name=data.get("repo_name", ""),
        local_path=repo_path,
        llvm_dep_type=llvm.get("dep_type", "submodule"),
        llvm_dep_path=llvm.get("dep_path", "llvm"),
        llvm_upstream_github=upstream_github,
        llvm_upstream_branch=upstream_branch,
        llvm_upstream_url=upstream_url,
        clean_commands=build.get("clean", ["rm -rf build"]),
        build_commands=build.get("commands", []),
        test_commands=test.get("commands", []),
        build_dir=build.get("dir", "build"),
        env=data.get("env", {}),
        timeout_build=build.get("timeout", 3600),
        timeout_test=test.get("timeout", 1800),
    )


def load_builtin_config(repo_name: str) -> Optional[dict[str, Any]]:
    """Look up a built-in YAML config by *repo_name* (matches part after last '/')."""
    short = repo_name.rsplit("/", 1)[-1]
    path = _BUILTIN_DIR / f"{short}.yml"
    if path.is_file():
        logger.debug("Loading built-in config: %s", path)
        return load_repo_yaml(path)
    return None


def discover_repo_config(repo_dir: str | Path) -> Optional[dict[str, Any]]:
    """Check whether *repo_dir* contains a SyncBots repo config and load it."""
    for fname in REPO_CONFIG_FILENAMES:
        path = Path(repo_dir) / fname
        if path.is_file():
            logger.info("Found repo config: %s", path)
            return load_repo_yaml(path)
    return None
