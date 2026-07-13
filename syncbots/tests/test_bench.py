"""Unit tests for the benchmark harness (config parsing, metrics aggregation)."""

from __future__ import annotations

import json

import pytest

from syncbots.bench import (
    append_records,
    case_record,
    format_report,
    load_bench_config,
    summarize_records,
)
from syncbots.state import AgentIterationReport, UpgradeResult


def _result(status="pass", iterations=2, tokens=(100, 50)):
    return UpgradeResult(
        repo_name="test/repo", status=status, iterations_used=iterations,
        target_llvm_hash="b" * 40, duration_sec=12.3,
        agent_reports=[AgentIterationReport(
            iteration=0, input_tokens=tokens[0], output_tokens=tokens[1],
            total_tokens=tokens[0] + tokens[1],
        )],
    )


def test_load_bench_config_validates(tmp_path):
    p = tmp_path / "bench.yaml"
    p.write_text("cases:\n  - repo: buddy-mlir\n    target_llvm: abc123\n")
    cases = load_bench_config(str(p))
    assert cases[0]["name"] == "buddy-mlir"
    assert cases[0]["max_iterations"] == 5

    p.write_text("cases:\n  - repo: buddy-mlir\n")
    with pytest.raises(ValueError, match="target_llvm"):
        load_bench_config(str(p))

    p.write_text("cases: []\n")
    with pytest.raises(ValueError, match="No benchmark cases"):
        load_bench_config(str(p))


def test_case_record_flattens_metrics():
    rec = case_record("case1", _result())
    assert rec["passed"] is True
    assert rec["iterations"] == 2
    assert rec["total_tokens"] == 150
    assert rec["target_llvm"] == "b" * 12


def test_summarize_records():
    records = [
        case_record("a", _result(status="pass", iterations=2)),
        case_record("b", _result(status="pass", iterations=4)),
        case_record("c", _result(status="build_fail", iterations=6)),
    ]
    s = summarize_records(records)
    assert s["cases"] == 3
    assert s["passed"] == 2
    assert s["pass_rate"] == round(2 / 3, 3)
    assert s["avg_iterations_to_pass"] == 3.0
    assert s["total_tokens"] == 450


def test_summarize_records_all_failed():
    s = summarize_records([case_record("a", _result(status="test_fail"))])
    assert s["pass_rate"] == 0.0
    assert s["avg_iterations_to_pass"] is None


def test_append_records_jsonl(tmp_path):
    path = str(tmp_path / "bench" / "results.jsonl")
    records = [case_record("a", _result())]
    summary = summarize_records(records)
    append_records(path, records + [summary])
    append_records(path, records + [summary])  # history accumulates
    lines = [json.loads(x) for x in open(path, encoding="utf-8") if x.strip()]
    assert len(lines) == 4
    assert lines[1]["kind"] == "summary"


def test_format_report_readable():
    records = [case_record("case1", _result())]
    text = format_report(records, summarize_records(records))
    assert "[PASS] case1" in text
    assert "pass rate" in text
