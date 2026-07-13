"""Deterministic prescan: grep the downstream repo for affected API patterns.

This runs BEFORE the deep agent so the agent receives a concrete, grep-verified
work list (file:line locations) instead of having to blindly search. It also
provides a raw LLVM diff slice for the ``diff-digest`` subagent to summarize.

Unlike the original analyzer, this module does NOT parse LLM free-text with
regexes -- structured API change extraction is delegated to the diff-digest
subagent via ``response_format``. Here we only do deterministic grep work.
"""

from __future__ import annotations

import logging
import os
import subprocess

from .state import RepoInfo
from .tools.git_tools import git_cmd

logger = logging.getLogger(__name__)

MIN_PATTERN_LENGTH = 6

_GENERIC_PATTERNS = frozenset({
    "alignment", "mask", "allocator", "chipset", "fastmath", "offset", "offsets",
    "result", "results", "value", "values", "type", "types", "module", "block",
    "region", "pass", "context", "attr", "index", "init", "clone", "class",
    "uint32_t", "uint64_t", "int32_t", "int64_t", "bool", "void", "auto", "const",
    "static", "inline", "virtual", "error", "collapse", "tile_sizes", "graph",
    "deprecated", "loops", "isSigned", "operand", "operands", "successor",
    "successors", "attribute", "attributes", "builder", "rewriter", "location",
    "state", "status", "config", "prefix", "suffix", "target", "source", "input",
    "output", "name", "size", "count", "data", "info", "kind", "mode", "enable",
    "disable", "create", "destroy", "update", "remove", "width", "height", "depth",
    "length", "capacity", "priority", "StructType", "FuncOp", "ModuleOp", "ReturnOp",
})


def is_specific_pattern(pattern: str) -> bool:
    """Return True if a pattern is specific enough to grep without false hits."""
    pattern = pattern.strip()
    if len(pattern) < MIN_PATTERN_LENGTH:
        return False
    if pattern.lower() in _GENERIC_PATTERNS:
        return False
    return True


def _exclude_globs(repo: RepoInfo) -> list[str]:
    """Path globs to exclude from prescan (LLVM submodule, vendored, build out).

    Downstream repos often vendor whole third-party source trees (including full
    copies of LLVM/clang/qemu). Those must never be part of the work list -- e.g.
    buddy-mlir carries a second LLVM under ``thirdparty/riscv-gnu-toolchain/llvm``
    in addition to its top-level ``llvm`` submodule. We exclude the common vendor
    directory names (both ``third_party`` and ``thirdparty`` spellings) and any
    nested ``llvm``/``llvm-project`` trees, not just top-level ones.
    """
    globs = [
        "!build/**", "!_build/**", "!.git/**", "!**/.git/**",
        "!third_party/**", "!thirdparty/**", "!third-party/**", "!externals/**",
        # Any vendored LLVM/clang tree at any depth (covers nested vendor copies).
        "!**/llvm-project/**", "!**/riscv-gnu-toolchain/**",
    ]
    dep = (repo.llvm_dep_path or "").strip().strip("/")
    if dep:
        globs.append(f"!{dep}/**")
    for candidate in ("llvm", "llvm-project", ".llvm-project"):
        if os.path.isdir(os.path.join(repo.local_path, candidate)):
            globs.append(f"!{candidate}/**")
    seen: set[str] = set()
    result: list[str] = []
    for g in globs:
        if g not in seen:
            seen.add(g)
            result.append(g)
    return result


def grep_repo(repo: RepoInfo, patterns: list[str], max_per_pattern: int = 30) -> dict[str, list[str]]:
    """Grep the downstream repo for each pattern; return {pattern: [locations]}.

    Locations are ``relpath:line`` strings, with excluded directories removed.
    Only patterns that pass :func:`is_specific_pattern` are searched.
    """
    repo_path = repo.local_path
    exclude = _exclude_globs(repo)
    exclude_prefixes = tuple(g[1:-3] for g in exclude if g.startswith("!") and g.endswith("/**"))
    hits: dict[str, list[str]] = {}

    for pat in patterns:
        if not is_specific_pattern(pat) or pat in hits:
            continue
        cmd = [
            "rg", "--no-heading", "-n", "--max-count", str(max_per_pattern),
            "--glob", "*.cpp", "--glob", "*.h", "--glob", "*.td",
            "--glob", "*.cmake", "--glob", "CMakeLists.txt",
        ]
        for g in exclude:
            cmd.extend(["--glob", g])
        cmd.extend(["-F", pat, repo_path])
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=15,
                encoding="utf-8", errors="replace",
            )
            raw = result.stdout.strip()
        except (FileNotFoundError, subprocess.SubprocessError):
            continue
        if not raw:
            continue
        locations: list[str] = []
        seen: set[str] = set()
        for line in raw.splitlines():
            rel = line.replace(repo_path + "/", "").replace(repo_path, "")
            if any(rel.startswith(p) for p in exclude_prefixes):
                continue
            # ripgrep output is "path:linenum:content" -> keep "path:linenum".
            fields = rel.split(":", 2)
            loc = f"{fields[0]}:{fields[1]}" if len(fields) >= 2 else fields[0]
            if loc not in seen:
                seen.add(loc)
                locations.append(loc)
        if locations:
            hits[pat] = locations
    return hits


def format_prescan_report(hits: dict[str, list[str]], max_locations: int = 15) -> str:
    """Render grep hits into a Markdown work list for the agent."""
    if not hits:
        return ""
    total = sum(len(v) for v in hits.values())
    lines = [
        f"## Verified affected locations ({total} file locations, {len(hits)} patterns)",
        "The patterns below were confirmed present in this repository by grep.",
        "Fix ONLY these; all other changelog entries had 0 hits and can be ignored.",
        "",
    ]
    for pat, locs in hits.items():
        lines.append(f"**`{pat}`**")
        for loc in locs[:max_locations]:
            lines.append(f"  {loc}")
        if len(locs) > max_locations:
            lines.append(f"  ... and {len(locs) - max_locations} more locations")
        lines.append("")
    return "\n".join(lines)


# ── Raw LLVM diff slice for the diff-digest subagent ─────────────────────────

_LLVM_DIFF_PATHS = [
    "mlir/include/mlir/IR",
    "mlir/include/mlir/Dialect",
    "mlir/include/mlir/Pass",
    "mlir/include/mlir/Interfaces",
    "mlir/include/mlir/Transforms",
    "mlir/include/mlir/Conversion",
    "llvm/include/llvm/IR",
    "llvm/include/llvm/ADT",
    "llvm/include/llvm/Support",
    "cmake/Modules",
    "mlir/cmake",
]


def extract_llvm_diff(llvm_repo: str, from_hash: str, to_hash: str, max_chars: int = 60000) -> str:
    """Extract a header-focused LLVM diff slice for the diff-digest subagent.

    The subagent reads this large blob in its own isolated context and returns a
    compact structured summary, so the main agent never sees the raw diff.
    """
    parts: list[str] = []
    total = 0
    for path in _LLVM_DIFF_PATHS:
        if total >= max_chars:
            break
        rc, out, _ = git_cmd(f"diff -U1 {from_hash}..{to_hash} -- {path}", llvm_repo, timeout=120)
        if rc != 0 or not out:
            continue
        chunk = f"### diff in {path}\n{out}"
        parts.append(chunk)
        total += len(chunk)
    blob = "\n\n".join(parts)
    return blob[:max_chars] if len(blob) > max_chars else blob
