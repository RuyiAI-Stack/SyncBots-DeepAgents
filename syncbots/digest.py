"""Runtime LLVM diff digest -- replaces the old static knowledge base.

Instead of relying on a pre-generated offline knowledge base of API-change
patterns, SyncBots now derives the breaking-change patterns at runtime by having
the ``diff-digest`` subagent read the actual LLVM diff between the current and
target commits and return a structured :class:`DiffDigest`.

The extracted ``grep_patterns`` are then deterministically grep-verified against
the downstream repo (see :mod:`syncbots.prescan`) to produce a concrete work
list for the main agent.

If no local llvm-project checkout is available, or the digest fails, this
degrades gracefully to an empty pattern list -- the main agent can still call
the ``diff-digest`` subagent itself during the loop.
"""

from __future__ import annotations

import logging
from typing import Optional

from .agent.prompts import DIFF_DIGEST_PROMPT
from .agent.schema import DiffDigest
from .llm import LLMConfigSet, create_model_for_role
from .prescan import extract_llvm_diff
from .scan import find_llvm_repo
from .state import RepoInfo
from .tools.llvm_tools import ensure_llvm_commits

logger = logging.getLogger(__name__)


def extract_api_change_patterns(
    repo: RepoInfo,
    llm_config: Optional[LLMConfigSet] = None,
    override_model: Optional[str] = None,
) -> list[str]:
    """Derive grep patterns for breaking API changes via the diff-digest agent.

    Returns a de-duplicated list of specific identifiers to grep for. Empty on
    any failure (no LLVM repo, no diff, or LLM error).
    """
    llvm_repo = find_llvm_repo([repo])
    if not llvm_repo or not repo.current_llvm_hash or not repo.target_llvm_hash:
        logger.info("[digest] no llvm-project / hashes; skipping runtime digest")
        return []
    if repo.current_llvm_hash == repo.target_llvm_hash:
        return []

    ensure_llvm_commits(
        llvm_repo,
        repo.current_llvm_hash,
        repo.target_llvm_hash,
        upstream_github=repo.llvm_upstream_github,
        upstream_branch=repo.llvm_upstream_branch,
    )

    diff_blob = extract_llvm_diff(llvm_repo, repo.current_llvm_hash, repo.target_llvm_hash)
    if not diff_blob.strip():
        logger.info("[digest] empty LLVM diff; nothing to digest")
        return []

    try:
        digest = _run_digest(diff_blob, llm_config, override_model)
    except Exception as e:  # noqa: BLE001
        logger.warning("[digest] diff-digest failed: %s", e)
        return []

    patterns: list[str] = []
    seen: set[str] = set()
    for change in digest.changes:
        for pat in [*change.grep_patterns, change.old_api]:
            pat = (pat or "").strip()
            if pat and pat not in seen:
                seen.add(pat)
                patterns.append(pat)
    logger.info("[digest] extracted %d candidate patterns from %d changes",
                len(patterns), len(digest.changes))
    return patterns


def _run_digest(
    diff_blob: str,
    llm_config: Optional[LLMConfigSet],
    override_model: Optional[str],
) -> DiffDigest:
    """Run a minimal one-shot deep agent to digest the diff into a DiffDigest.

    Does NOT use native structured output (``response_format``), since many
    self-hosted proxies do not support it. Instead the prompt asks for a JSON
    object, which we parse defensively from the reply text.
    """
    from deepagents import create_deep_agent
    from langchain_core.messages import AIMessage, HumanMessage

    from .agent.structured import parse_model

    model = create_model_for_role("diff_digest", llm_config, override_model)
    agent = create_deep_agent(model=model, system_prompt=DIFF_DIGEST_PROMPT)
    task = (
        "Digest the following LLVM/MLIR diff and return the breaking API changes "
        "downstream projects must adapt to. Reply with ONLY the JSON object.\n\n"
        "## LLVM diff\n" + diff_blob
    )
    out = agent.invoke({"messages": [HumanMessage(content=task)]})

    # Prefer native structured output if the proxy happened to provide it.
    result = out.get("structured_response") if isinstance(out, dict) else None
    if isinstance(result, DiffDigest):
        return result
    if isinstance(result, dict):
        parsed = parse_model_dict(result)
        if parsed is not None:
            return parsed

    # Fallback: parse JSON from the last assistant text message.
    messages = out.get("messages", []) if isinstance(out, dict) else []
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            text = msg.content if isinstance(msg.content, str) else str(msg.content)
            parsed = parse_model(text, DiffDigest)
            if parsed is not None:
                return parsed
            break
    logger.warning("[digest] could not parse DiffDigest JSON from model reply")
    return DiffDigest()


def parse_model_dict(data: dict) -> Optional[DiffDigest]:
    """Validate a dict into a DiffDigest, returning None on failure."""
    try:
        return DiffDigest(**data)
    except Exception:  # noqa: BLE001
        return None
