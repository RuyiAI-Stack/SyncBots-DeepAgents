"""Tests for agent trace formatting."""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from syncbots.agent.trace import extract_summary, format_transcript


def test_live_print_handles_none_update():
    from syncbots.agent.trace import _live_print

    _live_print("model", None)  # should not raise
    _live_print("model", {"messages": []})


def test_format_transcript_includes_tool_calls():
    msgs = [
        HumanMessage(content="fix the build"),
        AIMessage(content="I'll read the error log.", tool_calls=[{"name": "read_file", "args": {"path": "a.cpp"}, "id": "1"}]),
        ToolMessage(content="error on line 1", tool_call_id="1", name="read_file"),
        AIMessage(content="The fix is to rename getResult to getOpResult."),
    ]
    text = format_transcript(msgs, task="fix the build")
    assert "read_file" in text
    assert "getOpResult" in text


def test_extract_summary_returns_last_ai_text():
    msgs = [
        AIMessage(content="step one"),
        AIMessage(content="", tool_calls=[{"name": "grep", "args": {}, "id": "1"}]),
        AIMessage(content="final analysis here"),
    ]
    assert extract_summary(msgs) == "final analysis here"


def test_extract_summary_falls_back_to_tool_activity():
    msgs = [
        AIMessage(content="", tool_calls=[
            {"name": "edit_file", "args": {"file_path": "a.cpp"}, "id": "1"},
            {"name": "edit_file", "args": {"file_path": "b.cpp"}, "id": "2"},
        ]),
    ]
    summary = extract_summary(msgs)
    assert "edit_file" in summary
    assert "a.cpp" in summary and "b.cpp" in summary


def test_stringify_skips_tool_use_blocks():
    from syncbots.agent.trace import _stringify_content

    content = [
        {"type": "text", "text": "I'll call a tool."},
        {"type": "tool_use", "id": "x", "name": "ls", "input": {"path": "/tmp"}},
    ]
    out = _stringify_content(content)
    assert "I'll call a tool." in out
    assert "tool_use" not in out
    assert "/tmp" not in out
