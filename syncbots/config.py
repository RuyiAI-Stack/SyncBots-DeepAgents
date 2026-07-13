"""Repository configuration loader.

Supports loading from:
  1. Auto-discovery by scanning a directory for repos with LLVM dependencies
  2. Per-repo YAML config files (``.syncbots.yml`` or built-in ``repos/*.yml``)

The old JSON format (TEST_SYSTEM_REPOS.json) is no longer supported. Use YAML
config files instead (see ``syncbots/repos/*.yml`` for examples).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from .repo_config import (
    DIR_TO_REPO,
    KNOWN_LLVM_ANCHORS,
    _BUILTIN_DIR,
    discover_repo_config,
    load_builtin_config,
    load_repo_yaml,
    yaml_to_repo_info,
)
from .state import RepoInfo

logger = logging.getLogger(__name__)


def discover_repos(repos_dir: str | Path) -> list[RepoInfo]:
    """Auto-discover repos in a directory (user config > built-in > anchor heuristic)."""
    repos_dir = Path(repos_dir).resolve()
    repos: list[RepoInfo] = []

    for item in sorted(repos_dir.iterdir()):
        if not item.is_dir() or item.name.startswith("."):
            continue
        repo_path = str(item)

        user_cfg = discover_repo_config(item)
        if user_cfg:
            info = yaml_to_repo_info(user_cfg, repo_path)
            repos.append(info)
            logger.info("Discovered repo (user config): %s at %s", info.repo_name, item)
            continue

        repo_name = DIR_TO_REPO.get(item.name)
        if repo_name:
            builtin_cfg = load_builtin_config(repo_name)
            if builtin_cfg:
                info = yaml_to_repo_info(builtin_cfg, repo_path)
                repos.append(info)
                logger.info("Discovered repo (built-in config): %s at %s", info.repo_name, item)
                continue

        if not repo_name:
            continue
        anchor = next((a for a in KNOWN_LLVM_ANCHORS if a["repo_name"] == repo_name), None)
        if not anchor:
            continue

        dep_path = item / anchor["llvm_dep_path"]
        if anchor["llvm_dep_type"] == "submodule":
            has_dep = (dep_path / ".git").exists() or dep_path.is_dir()
        else:
            has_dep = dep_path.is_file()
        if not has_dep:
            logger.warning("Skipping %s: LLVM anchor not found at %s", item.name, dep_path)
            continue

        repos.append(
            RepoInfo(
                repo_name=repo_name,
                local_path=repo_path,
                llvm_dep_type=anchor["llvm_dep_type"],
                llvm_dep_path=anchor["llvm_dep_path"],
                llvm_upstream_github=anchor.get("llvm_upstream_github", ""),
                llvm_upstream_branch=anchor.get("llvm_upstream_branch", ""),
                llvm_upstream_url=anchor.get("llvm_upstream_url", ""),
            )
        )
        logger.info("Discovered repo (anchor heuristic): %s at %s", repo_name, item)

    return repos


def load_configs(
    repos_dir: Optional[str] = None,
    config_file: Optional[str] = None,
    repo_filter: Optional[list[str]] = None,
) -> list[RepoInfo]:
    """Load configs from auto-discovery, with optional filter.

    The *config_file* parameter (JSON format) is no longer supported and will
    raise an error directing users to migrate to YAML.
    """
    if config_file:
        raise ValueError(
            f"JSON config files are no longer supported: {config_file}\n"
            "Please migrate to YAML format. See syncbots/repos/*.yml for examples.\n"
            "Use --repos-dir to auto-discover repos, or --repo-config to point at a "
            ".syncbots.yml file."
        )
    if repos_dir:
        repos = discover_repos(repos_dir)
    else:
        raise ValueError("repos_dir must be provided (JSON config_file is no longer supported)")

    if repo_filter:
        normalized = {r.lower() for r in repo_filter}
        repos = [
            r for r in repos
            if r.repo_name.lower() in normalized
            or r.repo_name.split("/")[-1].lower() in normalized
        ]
    return repos
