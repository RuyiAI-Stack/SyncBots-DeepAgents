"""Pydantic schemas for structured subagent outputs.

These replace the original analyzer's hundreds of lines of regex-based parsing
of free-text LLM output. Subagents are configured with ``response_format`` so
the framework returns validated, structured data directly.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ApiChange(BaseModel):
    """One breaking API change extracted from an LLVM diff."""

    category: str = Field(description="One of: breaking, deprecation, build_system")
    component: str = Field(description="e.g. mlir, llvm, cmake, tablegen")
    summary: str = Field(description="One-line description of the change")
    old_api: str = Field(default="", description="Old API symbol/signature")
    new_api: str = Field(default="", description="New API symbol/signature")
    grep_patterns: list[str] = Field(
        default_factory=list,
        description="Specific identifiers to grep for in downstream code",
    )


class DiffDigest(BaseModel):
    """Structured summary of an LLVM diff, returned by the diff-digest subagent."""

    changes: list[ApiChange] = Field(default_factory=list)
    notes: str = Field(default="", description="Optional high-level notes for the coder")


class LogDiagnosis(BaseModel):
    """Root-cause diagnosis of a build/test log, returned by the log-analyst subagent."""

    error_type: str = Field(
        description="One of: compile_error, linker_error, tablegen_error, "
        "cmake_error, test_failure, unknown"
    )
    first_error_file: str = Field(default="", description="Path of the first failing file")
    first_error_line: int = Field(default=0, description="Line of the first error, if known")
    root_cause: str = Field(description="Concise explanation of the underlying cause")
    suggested_fix: str = Field(default="", description="Concrete next action for the coder")
    affected_files: list[str] = Field(
        default_factory=list, description="Files likely needing edits"
    )
