"""Build the main SyncBots deep agent.

The main agent operates directly on the real downstream repository via a
``LocalShellBackend`` (filesystem + shell). It delegates token-heavy work to
the context-isolation subagents and persists cross-iteration/cross-repo
learnings via ``MemoryMiddleware``.

The agent is created fresh per repo but its memory file persists on disk, so
later iterations and later repos benefit from earlier learnings.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from langchain_core.language_models import BaseChatModel

from ..llm import LLMConfigSet, create_model_for_role
from .prompts import MAIN_SYSTEM_PROMPT
from .subagents import build_subagents

logger = logging.getLogger(__name__)

DEFAULT_MEMORY_DIR = os.path.expanduser("~/.syncbots")
MEMORY_FILENAME = "AGENTS.md"

# Directory holding the packaged skills (each subdir has a SKILL.md).
SKILLS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills")


MAX_FREEFORM_MEMORY_CHARS = 24000  # cap for the agent-editable AGENTS.md


def _ensure_memory_file(memory_dir: str) -> str:
    """Ensure the free-form memory file exists (and is bounded); return its path."""
    Path(memory_dir).mkdir(parents=True, exist_ok=True)
    path = os.path.join(memory_dir, MEMORY_FILENAME)
    if not os.path.isfile(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(
                "# SyncBots upgrade memory\n\n"
                "Durable learnings about LLVM/MLIR upgrades: which fixes worked, "
                "which failed, and recurring API migration patterns.\n"
            )
        return path

    # Quality management: the agent appends freely, so trim when oversized
    # (keep the newest content -- old learnings are curated into MEMORY.md).
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        if len(content) > MAX_FREEFORM_MEMORY_CHARS:
            trimmed = content[-MAX_FREEFORM_MEMORY_CHARS:]
            nl = trimmed.find("\n")
            if 0 <= nl < 200:
                trimmed = trimmed[nl + 1:]
            with open(path, "w", encoding="utf-8") as f:
                f.write(
                    "# SyncBots upgrade memory\n\n"
                    "(older content trimmed; curated learnings live in MEMORY.md)\n\n"
                    + trimmed
                )
            logger.info("Trimmed oversized free-form memory file %s", path)
    except OSError as e:
        logger.warning("Could not check/trim memory file %s: %s", path, e)
    return path


def build_upgrade_agent(
    repo_path: str,
    llm_config: Optional[LLMConfigSet] = None,
    override_model: Optional[str] = None,
    memory_dir: str = DEFAULT_MEMORY_DIR,
    enable_memory: bool = True,
):
    """Create the main deep agent rooted at *repo_path*.

    Returns a compiled LangGraph agent (``CompiledStateGraph``) ready to
    ``invoke({"messages": [...]})``.
    """
    from deepagents import MemoryMiddleware, create_deep_agent
    from deepagents.backends import LocalShellBackend
    from deepagents.middleware.skills import SkillsMiddleware

    main_model: BaseChatModel = create_model_for_role("main", llm_config, override_model)
    diff_model = create_model_for_role("diff_digest", llm_config, override_model)
    log_model = create_model_for_role("log_analyst", llm_config, override_model)
    check_model = create_model_for_role("check_regen", llm_config, override_model)

    subagents = build_subagents(diff_model, log_model, check_model)

    # LocalShellBackend gives the agent real filesystem + shell access rooted at
    # the repo. inherit_env=True so opt tools on PATH and build env are visible.
    # virtual_mode=False: operate on real repo paths (no sandbox; trusted local use).
    backend = LocalShellBackend(
        root_dir=repo_path, virtual_mode=False, inherit_env=True, timeout=600
    )

    middleware = []

    # Skills: packaged LLVM fix-strategy playbooks, loaded via progressive
    # disclosure (agent sees name+description, reads full SKILL.md on demand).
    if os.path.isdir(SKILLS_DIR):
        middleware.append(
            SkillsMiddleware(
                backend=LocalShellBackend(virtual_mode=False, inherit_env=True),
                sources=[(SKILLS_DIR, "SyncBots")],
            )
        )
        logger.info("Agent skills enabled from %s", SKILLS_DIR)

    if enable_memory:
        from ..memory import MemoryStore

        memory_path = _ensure_memory_file(memory_dir)
        sources = []
        # Curated, quality-managed knowledge rendered from the JSONL store
        # (verified-first, deduped, pruned) -- injected before the free-form file.
        curated_path = MemoryStore(memory_dir).write_rendered()
        if curated_path:
            sources.append(curated_path)
        sources.append(memory_path)
        middleware.append(
            MemoryMiddleware(
                backend=LocalShellBackend(virtual_mode=False, inherit_env=True),
                sources=sources,
            )
        )
        logger.info("Agent memory enabled: %s", ", ".join(sources))

    agent = create_deep_agent(
        model=main_model,
        system_prompt=MAIN_SYSTEM_PROMPT,
        subagents=subagents,
        middleware=middleware,
        backend=backend,
    )
    return agent
