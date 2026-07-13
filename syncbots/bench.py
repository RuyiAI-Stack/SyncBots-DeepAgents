"""Benchmark harness: fixed (repo, base_llvm, target_llvm) cases with metrics.

Gives prompt/skill changes a measurable footing: each run of ``syncbots bench``
executes the configured upgrade cases and appends one JSON line per case (plus
one summary line) to a results file, tracking the three key metrics over time:

- pass rate
- average iterations to pass
- token consumption (input/output/total)

Benchmark config (YAML)::

    cases:
      - name: buddy-19-to-20        # optional; defaults to repo name
        repo: buddy-mlir            # repo name (builtin config lookup)
        repo_path: ../buddy-mlir    # local checkout
        base_ref: main              # optional
        base_llvm: <hash>           # optional
        target_llvm: <hash>         # required
        max_iterations: 5           # optional (default 5)

This module holds the pure, unit-testable parts (config parsing, record
building, aggregation, persistence); the CLI command drives the actual runs.
"""

from __future__ import annotations

import datetime
import json
import os
from typing import Any

from .state import UpgradeResult

DEFAULT_MAX_ITERATIONS = 5
RESULTS_FILENAME = "bench-results.jsonl"


def load_bench_config(path: str) -> list[dict[str, Any]]:
    """Load and validate the benchmark case list from a YAML file."""
    import yaml

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    cases = data.get("cases") or []
    if not isinstance(cases, list) or not cases:
        raise ValueError(f"No benchmark cases found in {path} (expected top-level 'cases' list)")
    out: list[dict[str, Any]] = []
    for i, case in enumerate(cases):
        if not isinstance(case, dict):
            raise ValueError(f"Case #{i} is not a mapping")
        if not case.get("repo"):
            raise ValueError(f"Case #{i} is missing required field 'repo'")
        if not case.get("target_llvm"):
            raise ValueError(f"Case '{case.get('name') or case['repo']}' is missing required field 'target_llvm'")
        case.setdefault("name", case["repo"])
        case.setdefault("max_iterations", DEFAULT_MAX_ITERATIONS)
        out.append(case)
    return out


def case_record(name: str, result: UpgradeResult) -> dict[str, Any]:
    """Build one flat, JSONL-friendly metrics record from an upgrade result."""
    tokens = result.token_totals()
    return {
        "kind": "case",
        "timestamp": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "name": name,
        "repo": result.repo_name,
        "status": result.status,
        "passed": result.status == "pass",
        "iterations": result.iterations_used,
        "duration_sec": round(result.duration_sec, 1),
        "input_tokens": tokens["input_tokens"],
        "output_tokens": tokens["output_tokens"],
        "total_tokens": tokens["total_tokens"],
        "target_llvm": result.target_llvm_hash[:12],
        "segments_total": result.segments_total,
        "segments_completed": result.segments_completed,
        "log_dir": result.log_dir,
        "error": result.error_message,
    }


def summarize_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate case records into the headline benchmark metrics."""
    cases = [r for r in records if r.get("kind") == "case"]
    n = len(cases)
    passed = [r for r in cases if r.get("passed")]
    summary: dict[str, Any] = {
        "kind": "summary",
        "timestamp": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cases": n,
        "passed": len(passed),
        "pass_rate": round(len(passed) / n, 3) if n else 0.0,
        "avg_iterations_to_pass": (
            round(sum(r["iterations"] for r in passed) / len(passed), 2) if passed else None
        ),
        "total_tokens": sum(r.get("total_tokens", 0) for r in cases),
        "input_tokens": sum(r.get("input_tokens", 0) for r in cases),
        "output_tokens": sum(r.get("output_tokens", 0) for r in cases),
        "total_duration_sec": round(sum(r.get("duration_sec", 0.0) for r in cases), 1),
    }
    return summary


def append_records(path: str, records: list[dict[str, Any]]) -> None:
    """Append records to the JSONL results file (history accumulates over runs)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def format_report(records: list[dict[str, Any]], summary: dict[str, Any]) -> str:
    """Human-readable benchmark report for the terminal."""
    lines = ["", "Benchmark results:"]
    for r in records:
        if r.get("kind") != "case":
            continue
        mark = "PASS" if r.get("passed") else r.get("status", "fail").upper()
        lines.append(
            f"  [{mark}] {r['name']}: {r['iterations']} iter, "
            f"{r['total_tokens']} tokens, {r['duration_sec']}s"
        )
    avg_iter = summary.get("avg_iterations_to_pass")
    lines += [
        "",
        f"  pass rate:      {summary['passed']}/{summary['cases']} ({summary['pass_rate']:.0%})",
        f"  avg iterations: {avg_iter if avg_iter is not None else 'n/a'} (passing cases)",
        f"  tokens:         in={summary['input_tokens']} out={summary['output_tokens']} "
        f"total={summary['total_tokens']}",
        f"  wall time:      {summary['total_duration_sec']}s",
    ]
    return "\n".join(lines)
