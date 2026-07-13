"""SyncBots: Deep-agent based LLVM/MLIR automatic upgrade tool.

A rewrite of the original LangGraph + claude-code pipeline on top of the
``deepagents`` subagent framework. The architecture has two layers:

- An outer deterministic *loop engineering* controller
  (:mod:`syncbots.loop.controller`) that drives a deep agent in a fix loop and
  treats **unit tests passing** as the sole success-exit condition.
- An inner deep agent (:mod:`syncbots.agent.builder`) that plans and edits code,
  delegating token-heavy work (huge diffs, long build logs, CHECK regeneration)
  to context-isolated subagents. Breaking-change patterns are derived at runtime
  by the diff-digest subagent rather than from any static knowledge base.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
