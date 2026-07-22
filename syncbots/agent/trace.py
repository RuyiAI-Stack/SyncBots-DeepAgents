"""Capture and format deep-agent analysis output for the user.

Each loop iteration produces a markdown transcript (tool calls, subagent
results, final assistant text) saved under the unified output root
``<output_root>/<repo_name>/<run-id>/`` (see :mod:`syncbots.paths`).
"""

from __future__ import annotations

import datetime
import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from ..state import AgentIterationReport

logger = logging.getLogger(__name__)

MAX_TOOL_OUTPUT = 4000
MAX_CONTENT = 8000


def make_run_log_dir(
    repo_path: str,
    explicit: Optional[str] = None,
    repo_name: str = "",
    output_root: Optional[str] = None,
) -> str:
    """Create and return the unified directory for one upgrade run's artifacts.

    Layout: ``<output_root>/<repo_name>/<timestamp>/`` with a ``logs/`` subdir
    for full build/test logs. Falls back to the repo dir name if *repo_name* is
    empty. An *explicit* path overrides everything.
    """
    from ..paths import default_output_root

    if explicit:
        path = Path(explicit)
    else:
        root = Path(output_root) if output_root else Path(default_output_root())
        safe_repo = (repo_name or Path(repo_path).name).strip("/") or "repo"
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        path = root / safe_repo / ts
    path.mkdir(parents=True, exist_ok=True)
    (path / "logs").mkdir(parents=True, exist_ok=True)
    return str(path)


def write_text(path: str, content: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _truncate(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: limit - 20] + "\n... [truncated] ..."


def _stringify_content(content: Any) -> str:
    """Extract human-readable TEXT from a message content (skip tool_use blocks)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                btype = block.get("type")
                if btype == "text":
                    parts.append(str(block.get("text", "")))
                elif btype in ("tool_use", "tool_result", "thinking", "input_json_delta"):
                    # Tool calls are rendered separately; don't dump raw JSON here.
                    continue
                elif "text" in block:
                    parts.append(str(block.get("text", "")))
            # Non-text blocks (e.g. objects) are intentionally skipped.
        return "\n".join(p for p in parts if p)
    return str(content)


def format_message(msg: BaseMessage, index: int) -> str:
    """Render one LangChain message as markdown."""
    role = type(msg).__name__
    lines = [f"### [{index}] {role}"]

    if isinstance(msg, HumanMessage):
        lines.append(_truncate(_stringify_content(msg.content), MAX_CONTENT))
        return "\n\n".join(lines) + "\n"

    if isinstance(msg, AIMessage):
        text = _truncate(_stringify_content(msg.content), MAX_CONTENT)
        if text.strip():
            lines.append(text)
        tool_calls = getattr(msg, "tool_calls", None) or []
        for tc in tool_calls:
            name = tc.get("name", "?") if isinstance(tc, dict) else getattr(tc, "name", "?")
            args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
            lines.append(f"**tool_call** `{name}`\n```json\n{json.dumps(args, ensure_ascii=False, indent=2)}\n```")
        return "\n\n".join(lines) + "\n"

    if isinstance(msg, ToolMessage):
        name = getattr(msg, "name", None) or "tool"
        lines.append(f"**tool** `{name}`")
        lines.append("```")
        lines.append(_truncate(_stringify_content(msg.content), MAX_TOOL_OUTPUT))
        lines.append("```")
        return "\n\n".join(lines) + "\n"

    lines.append(_truncate(_stringify_content(getattr(msg, "content", "")), MAX_CONTENT))
    return "\n\n".join(lines) + "\n"


def format_transcript(messages: list[BaseMessage], task: str = "") -> str:
    """Build a full markdown transcript from agent messages."""
    parts = ["# Agent transcript", ""]
    if task:
        parts += ["## Task", "", task, ""]
    parts.append("## Messages")
    parts.append("")
    for i, msg in enumerate(messages):
        parts.append(format_message(msg, i))
    return "\n".join(parts)


def _tool_call_name(tc: Any) -> str:
    if isinstance(tc, dict):
        return tc.get("name", "?")
    return getattr(tc, "name", "?")


def extract_summary(messages: list[BaseMessage]) -> str:
    """Summarize the iteration: last assistant text, or tool activity as fallback."""
    for msg in reversed(messages):
        if not isinstance(msg, AIMessage):
            continue
        text = _stringify_content(msg.content).strip()
        if text:
            return text

    # No final assistant prose -- summarize what the agent actually did.
    from collections import Counter

    tool_counts: Counter = Counter()
    edited_files: list[str] = []
    for msg in messages:
        if not isinstance(msg, AIMessage):
            continue
        for tc in getattr(msg, "tool_calls", None) or []:
            name = _tool_call_name(tc)
            tool_counts[name] += 1
            if name in ("edit_file", "write_file"):
                args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
                path = args.get("file_path") or args.get("path") if isinstance(args, dict) else None
                if path:
                    edited_files.append(str(path))

    if tool_counts:
        parts = ["Agent ran tools: " + ", ".join(f"{n}×{c}" for n, c in tool_counts.most_common())]
        if edited_files:
            parts.append("Edited files: " + ", ".join(sorted(set(edited_files))))
        return "\n".join(parts)
    return "(no assistant text in this iteration)"


def _collect_usage(callback: Any, messages: list[BaseMessage]) -> tuple[int, int, int]:
    """Aggregate (input, output, total) token usage for one agent run.

    Primary source: the ``UsageMetadataCallbackHandler`` attached to the run,
    which sees every model call including subagent calls. Fallback: sum the
    ``usage_metadata`` of the AIMessages in the top-level conversation (main
    agent only).
    """
    usage = getattr(callback, "usage_metadata", None) or {}
    inp = sum(int(u.get("input_tokens", 0) or 0) for u in usage.values())
    out = sum(int(u.get("output_tokens", 0) or 0) for u in usage.values())
    tot = sum(int(u.get("total_tokens", 0) or 0) for u in usage.values())
    if tot or inp or out:
        return inp, out, tot or (inp + out)

    for msg in messages:
        if not isinstance(msg, AIMessage):
            continue
        u = getattr(msg, "usage_metadata", None) or {}
        inp += int(u.get("input_tokens", 0) or 0)
        out += int(u.get("output_tokens", 0) or 0)
        tot += int(u.get("total_tokens", 0) or 0)
    return inp, out, tot or (inp + out)


def _live_print(node: str, update: Any) -> None:
    """Print high-signal agent steps to the terminal."""
    if not isinstance(update, dict):
        return
    msgs = update.get("messages")
    if not msgs:
        return
    new_msgs = msgs if isinstance(msgs, list) else [msgs]
    for msg in new_msgs:
        if isinstance(msg, AIMessage):
            text = _stringify_content(msg.content).strip()
            if text:
                logger.info("[agent] %s\n%s", node, _truncate(text, 1200))
            for tc in getattr(msg, "tool_calls", None) or []:
                name = tc.get("name", "?") if isinstance(tc, dict) else getattr(tc, "name", "?")
                logger.info("[agent] %s -> tool `%s`", node, name)
        elif isinstance(msg, ToolMessage):
            name = getattr(msg, "name", None) or "tool"
            preview = _truncate(_stringify_content(msg.content), 400).replace("\n", " ")
            logger.info("[agent] %s <- `%s`: %s", node, name, preview)


def invoke_and_trace(
    agent,
    task: str,
    iteration: int = 0,
    *,
    show_live: bool = False,
    log_dir: Optional[str] = None,
) -> AgentIterationReport:
    """Run the agent, capture messages, optionally stream to the terminal, save report."""
    from langchain_core.callbacks import UsageMetadataCallbackHandler

    input_state = {"messages": [HumanMessage(content=task)]}
    messages: list[BaseMessage] = []
    error = ""
    usage_cb = UsageMetadataCallbackHandler()
    run_config = {"callbacks": [usage_cb]}

    try:
        if show_live:
            logger.info("[agent] iteration %d starting (live trace on)", iteration)
            seen_ids: set[int] = set()
            # "updates" yields {node: state_delta} per superstep; accumulate the
            # new messages so we reconstruct the full conversation while also
            # printing each step live. This is the standard, version-stable mode.
            for event in agent.stream(input_state, config=run_config, stream_mode="updates", subgraphs=True):
                # With subgraphs=True, events are (namespace_tuple, update_dict).
                if isinstance(event, tuple) and len(event) == 2 and isinstance(event[1], dict):
                    update_dict = event[1]
                elif isinstance(event, dict):
                    update_dict = event
                else:
                    continue
                for node, update in update_dict.items():
                    _live_print(node, update)
                    if not isinstance(update, dict):
                        continue
                    new = update.get("messages")
                    if not new:
                        continue
                    new_list = new if isinstance(new, list) else [new]
                    for m in new_list:
                        mid = id(m)
                        if mid not in seen_ids:
                            seen_ids.add(mid)
                            messages.append(m)
        else:
            final = agent.invoke(input_state, config=run_config)
            messages = final.get("messages", [])
    except Exception as e:  # noqa: BLE001
        error = str(e)
        logger.error("[agent] iteration %d failed: %s", iteration, e)

    summary = extract_summary(messages) if messages else f"(agent error: {error})" if error else "(empty)"
    transcript = format_transcript(messages, task) if messages else f"# Agent transcript\n\nAgent error: {error}\n"
    report_path = ""
    in_tok, out_tok, total_tok = _collect_usage(usage_cb, messages)
    if total_tok:
        logger.info(
            "[agent] iteration %d tokens: in=%d out=%d total=%d",
            iteration, in_tok, out_tok, total_tok,
        )

    if log_dir:
        report_path = os.path.join(log_dir, f"iteration-{iteration}.md")
        write_text(os.path.join(log_dir, f"iteration-{iteration}-summary.md"), f"# Summary (iteration {iteration})\n\n{summary}\n")
        logger.info("[agent] saved summary: %s", report_path)

    if not show_live and messages:
        logger.info("[agent] iteration %d summary:\n%s", iteration, _truncate(summary, 2000))

    return AgentIterationReport(
        iteration=iteration,
        summary=summary,
        transcript_path=report_path,
        message_count=len(messages),
        error=error,
        input_tokens=in_tok,
        output_tokens=out_tok,
        total_tokens=total_tok,
    )
