"""Centralized output/log path resolution.

All run artifacts (agent transcripts, prescan reports, and full build/test
logs) live under a single output root so they are easy to find and inspect:

    <output_root>/<repo_name>/<timestamp>/
        prescan.md
        iteration-N.md
        iteration-N-summary.md
        summary.md
        logs/
            iter0_build.log
            iter0_test.log
            ...

The default output root is ``SyncBotsDeep/output`` (next to the package). It can
be overridden via the ``SYNCBOTS_OUTPUT_DIR`` environment variable or a CLI flag.
"""

from __future__ import annotations

import os
from pathlib import Path


def package_root() -> Path:
    """Return the SyncBotsDeep project root (parent of the ``syncbots`` package)."""
    return Path(__file__).resolve().parents[1]


def default_output_root() -> str:
    """Return the default output root, honoring ``SYNCBOTS_OUTPUT_DIR``."""
    env = os.environ.get("SYNCBOTS_OUTPUT_DIR")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return str(package_root() / "output")
