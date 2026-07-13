"""Tests for defensive JSON parsing of LLM structured output."""

from __future__ import annotations

from syncbots.agent.schema import DiffDigest, LogDiagnosis
from syncbots.agent.structured import parse_json, parse_model


def test_parse_plain_json_object():
    assert parse_json('{"a": 1}') == {"a": 1}


def test_parse_fenced_json():
    text = "Here is the result:\n```json\n{\"changes\": [], \"notes\": \"ok\"}\n```\nDone."
    data = parse_json(text)
    assert data == {"changes": [], "notes": "ok"}


def test_parse_json_with_surrounding_prose():
    text = 'Sure! {"error_type": "compile_error", "root_cause": "x"} hope that helps'
    data = parse_json(text)
    assert data["error_type"] == "compile_error"


def test_parse_model_diff_digest():
    text = """```json
    {"changes": [{"category": "breaking", "component": "mlir", "summary": "renamed",
      "old_api": "getResult", "new_api": "getOpResult", "grep_patterns": ["getResult"]}],
     "notes": ""}
    ```"""
    digest = parse_model(text, DiffDigest)
    assert digest is not None
    assert len(digest.changes) == 1
    assert digest.changes[0].grep_patterns == ["getResult"]


def test_parse_model_log_diagnosis():
    text = '{"error_type": "linker_error", "root_cause": "undefined ref", "affected_files": ["a.cpp"]}'
    diag = parse_model(text, LogDiagnosis)
    assert diag is not None
    assert diag.error_type == "linker_error"
    assert diag.affected_files == ["a.cpp"]


def test_parse_model_returns_none_on_garbage():
    assert parse_model("no json here at all", DiffDigest) is None
