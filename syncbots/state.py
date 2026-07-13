"""Core data models for the SyncBots upgrade pipeline.

These are plain dataclasses shared across the deterministic modules (scan,
verify, prescan) and the loop controller. Unlike the original project, there is
no LangGraph ``TypedDict`` state flowing through a graph -- the deep agent owns
its own conversation state, and the loop controller owns the upgrade state via
these dataclasses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class RepoStatus(str, Enum):
    PENDING = "pending"
    UPGRADING = "upgrading"
    BUILD_OK = "build_ok"
    TEST_OK = "test_ok"
    PASS = "pass"
    BUILD_FAIL = "build_fail"
    TEST_FAIL = "test_fail"
    UPGRADE_FAIL = "upgrade_fail"
    SKIPPED = "skipped"


@dataclass
class RepoInfo:
    """Information about one downstream repository."""

    repo_name: str
    local_path: str
    llvm_dep_type: str  # "submodule" | "hash_file"
    llvm_dep_path: str
    llvm_upstream_github: str = ""  # e.g. "RuyiAI-Stack/llvm-project" (non-upstream LLVM fork)
    llvm_upstream_branch: str = ""  # default branch for target resolution, e.g. "riscv"
    llvm_upstream_url: str = ""  # optional canonical URL (informational / logging)
    base_ref: str = ""
    base_llvm: str = ""
    current_llvm_hash: str = ""
    current_llvm_version: str = ""
    target_llvm_hash: str = ""
    upgrade_type: str = "unknown"
    status: str = "pending"
    clean_commands: list[str] = field(default_factory=list)
    build_commands: list[str] = field(default_factory=list)
    test_commands: list[str] = field(default_factory=list)
    build_dir: str = "build"
    env: dict[str, str] = field(default_factory=dict)
    timeout_build: int = 3600
    timeout_test: int = 1800

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_name": self.repo_name,
            "local_path": self.local_path,
            "llvm_dep_type": self.llvm_dep_type,
            "llvm_dep_path": self.llvm_dep_path,
            "llvm_upstream_github": self.llvm_upstream_github,
            "llvm_upstream_branch": self.llvm_upstream_branch,
            "llvm_upstream_url": self.llvm_upstream_url,
            "base_ref": self.base_ref,
            "base_llvm": self.base_llvm,
            "current_llvm_hash": self.current_llvm_hash,
            "current_llvm_version": self.current_llvm_version,
            "target_llvm_hash": self.target_llvm_hash,
            "upgrade_type": self.upgrade_type,
            "status": self.status,
            "clean_commands": self.clean_commands,
            "build_commands": self.build_commands,
            "test_commands": self.test_commands,
            "build_dir": self.build_dir,
            "env": self.env,
            "timeout_build": self.timeout_build,
            "timeout_test": self.timeout_test,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RepoInfo":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class VerifyResult:
    """Outcome of one build+test verification pass.

    ``tests_passed`` is the sole success signal for the loop controller.
    """

    build_ok: bool
    tests_passed: bool
    phase: str  # "build" | "test" | "done"
    error_type: str = "unknown"
    error_summary: str = ""
    duration_sec: float = 0.0
    log_path: str = ""  # path to the full build/test log for this phase

    @property
    def success(self) -> bool:
        return self.build_ok and self.tests_passed

    def to_dict(self) -> dict[str, Any]:
        return {
            "build_ok": self.build_ok,
            "tests_passed": self.tests_passed,
            "phase": self.phase,
            "log_path": self.log_path,
            "error_type": self.error_type,
            "error_summary": self.error_summary,
            "duration_sec": round(self.duration_sec, 2),
        }


@dataclass
class FixAttempt:
    """Record of one loop iteration's diagnosis, used for de-duplication."""

    iteration: int
    phase: str
    error_type: str
    error_summary: str
    diff: str = ""
    fingerprint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "iteration": self.iteration,
            "phase": self.phase,
            "error_type": self.error_type,
            "fingerprint": self.fingerprint,
        }


@dataclass
class AgentIterationReport:
    """Captured analysis output from one agent invocation."""

    iteration: int
    summary: str = ""
    transcript_path: str = ""
    message_count: int = 0
    error: str = ""
    # Aggregated LLM usage for this iteration (all model calls, incl. subagents).
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "iteration": self.iteration,
            "summary": self.summary,
            "transcript_path": self.transcript_path,
            "message_count": self.message_count,
            "error": self.error,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass
class UpgradeResult:
    """Final result for one repository upgrade attempt."""

    repo_name: str
    status: str
    iterations_used: int = 0
    target_llvm_hash: str = ""
    history: list[FixAttempt] = field(default_factory=list)
    error_message: str = ""
    agent_reports: list[AgentIterationReport] = field(default_factory=list)
    log_dir: str = ""
    duration_sec: float = 0.0
    # Staged (segmented) upgrades: how many segments were planned / passed.
    segments_total: int = 1
    segments_completed: int = 0

    def token_totals(self) -> dict[str, int]:
        """Aggregate LLM usage across all iterations."""
        return {
            "input_tokens": sum(r.input_tokens for r in self.agent_reports),
            "output_tokens": sum(r.output_tokens for r in self.agent_reports),
            "total_tokens": sum(r.total_tokens for r in self.agent_reports),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_name": self.repo_name,
            "status": self.status,
            "iterations_used": self.iterations_used,
            "target_llvm_hash": self.target_llvm_hash,
            "history": [h.to_dict() for h in self.history],
            "error_message": self.error_message,
            "agent_reports": [r.to_dict() for r in self.agent_reports],
            "log_dir": self.log_dir,
            "duration_sec": round(self.duration_sec, 2),
            "tokens": self.token_totals(),
            "segments_total": self.segments_total,
            "segments_completed": self.segments_completed,
        }
