"""Context-isolation subagents for the SyncBots deep agent.

The topology is organised around *context isolation* rather than coding roles:
each subagent absorbs a token-heavy task in its own isolated context and returns
only a compact, structured result to the main agent.

- diff-digest: reads giant LLVM diffs    -> DiffDigest (structured API changes)
- log-analyst: reads long build/test logs -> LogDiagnosis (root cause)
- check-regen: runs opt tools             -> corrected CHECK lines (text)
"""

from __future__ import annotations

from typing import Optional

from langchain_core.language_models import BaseChatModel

from .prompts import CHECK_REGEN_PROMPT, DIFF_DIGEST_PROMPT, LOG_ANALYST_PROMPT


def build_subagents(
    diff_model: Optional[BaseChatModel] = None,
    log_model: Optional[BaseChatModel] = None,
    check_model: Optional[BaseChatModel] = None,
) -> list[dict]:
    """Build the SubAgent specs. Models are optional; omitted ones inherit main's.

    Each entry is a ``SubAgent`` TypedDict. Tools are inherited from the main
    agent (filesystem + execute) unless overridden.

    Note: we deliberately do NOT set ``response_format`` here. Many self-hosted
    Anthropic/OpenAI-compatible proxies do not support native structured output,
    which makes the subagent crash. Instead the prompts ask the model to emit a
    JSON object as its final message, which the main agent reads as text (and
    :mod:`syncbots.digest` parses defensively via :mod:`syncbots.agent.structured`).
    """
    diff_digest: dict = {
        "name": "diff-digest",
        "description": (
            "Read a large LLVM/MLIR diff and return the breaking API changes "
            "downstream code must adapt to (as a JSON object). Use this instead "
            "of reading raw diffs yourself. Input: the from/to LLVM hashes or a "
            "pasted diff."
        ),
        "system_prompt": DIFF_DIGEST_PROMPT,
    }
    log_analyst: dict = {
        "name": "log-analyst",
        "description": (
            "Read a long build or test log and return a root-cause diagnosis "
            "as a JSON object (error type, first failing file, suggested fix). "
            "Use this when a log is long or confusing."
        ),
        "system_prompt": LOG_ANALYST_PROMPT,
    }
    check_regen: dict = {
        "name": "check-regen",
        "description": (
            "Fix FileCheck test failures: run each failing test's RUN command and "
            "rewrite its CHECK lines to match the new tool output. Applies edits "
            "directly and reports which files changed."
        ),
        "system_prompt": CHECK_REGEN_PROMPT,
    }

    if diff_model is not None:
        diff_digest["model"] = diff_model
    if log_model is not None:
        log_analyst["model"] = log_model
    if check_model is not None:
        check_regen["model"] = check_model

    return [diff_digest, log_analyst, check_regen]
