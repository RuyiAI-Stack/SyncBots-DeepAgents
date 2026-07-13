"""Structured cross-run memory with quality management.

The raw ``~/.syncbots/AGENTS.md`` free-form memory (agent-editable via
``MemoryMiddleware``) grows without bound and mixes stale and current
knowledge. This module adds a curated layer on top:

- Entries live in a JSONL store (``~/.syncbots/memory.jsonl``) with metadata:
  repo, LLVM hash range, error type, verified flag, timestamps.
- Deduplication: entries with the same normalized lesson collapse into one
  (the timestamp is refreshed instead).
- Pruning at render time: per-error-type caps, verified entries preferred,
  stale unverified entries dropped.
- ``render_memory_file`` composes a compact ``MEMORY.md`` that is injected as
  a read-only memory source alongside the free-form AGENTS.md.

The controller archives learnings after each run via
:func:`record_run_learnings`: on PASS, the iteration summaries that resolved
each failure become *verified* entries; on abort, the unresolved failure is
recorded as a negative (unverified) entry so future runs avoid the same
dead end.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .state import RepoInfo, UpgradeResult

logger = logging.getLogger(__name__)

DEFAULT_MEMORY_DIR = os.path.expanduser("~/.syncbots")
STORE_FILENAME = "memory.jsonl"
RENDERED_FILENAME = "MEMORY.md"

MAX_ENTRIES_PER_TYPE = 8          # rendered entries per error_type bucket
MAX_UNVERIFIED_AGE_DAYS = 90      # unverified entries older than this are dropped
MAX_LESSON_CHARS = 700            # stored lesson length cap


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _lesson_fingerprint(lesson: str) -> str:
    """Stable fingerprint of a lesson for de-duplication."""
    norm = re.sub(r"\s+", " ", lesson.lower()).strip()
    norm = re.sub(r"\b[0-9a-f]{7,40}\b", "HASH", norm)
    norm = re.sub(r"\b\d{3,}\b", "N", norm)
    return hashlib.sha1(norm.encode("utf-8", "replace")).hexdigest()[:16]


@dataclass
class MemoryEntry:
    """One durable learning from an upgrade run."""

    lesson: str
    error_type: str = "unknown"
    repo: str = ""
    llvm_from: str = ""
    llvm_to: str = ""
    verified: bool = False  # True when it comes from a run that ended in PASS
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    fingerprint: str = ""

    def __post_init__(self) -> None:
        self.lesson = (self.lesson or "").strip()[:MAX_LESSON_CHARS]
        if not self.fingerprint:
            self.fingerprint = _lesson_fingerprint(self.lesson)

    def age_days(self) -> float:
        try:
            updated = datetime.datetime.strptime(self.updated_at, "%Y-%m-%dT%H:%M:%SZ")
            updated = updated.replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            return 0.0
        return (datetime.datetime.now(datetime.timezone.utc) - updated).total_seconds() / 86400.0


class MemoryStore:
    """JSONL-backed store of :class:`MemoryEntry` with dedup and pruning."""

    def __init__(self, memory_dir: str = DEFAULT_MEMORY_DIR):
        self.memory_dir = memory_dir
        self.store_path = os.path.join(memory_dir, STORE_FILENAME)
        self.rendered_path = os.path.join(memory_dir, RENDERED_FILENAME)

    # ── persistence ──────────────────────────────────────────────────────

    def load(self) -> list[MemoryEntry]:
        entries: list[MemoryEntry] = []
        if not os.path.isfile(self.store_path):
            return entries
        with open(self.store_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    entries.append(MemoryEntry(**{
                        k: v for k, v in d.items()
                        if k in MemoryEntry.__dataclass_fields__
                    }))
                except (json.JSONDecodeError, TypeError) as e:
                    logger.warning("[memory] skipping bad entry: %s", e)
        return entries

    def _save(self, entries: list[MemoryEntry]) -> None:
        os.makedirs(self.memory_dir, exist_ok=True)
        with open(self.store_path, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(asdict(e), ensure_ascii=False) + "\n")

    # ── mutation ─────────────────────────────────────────────────────────

    def add(self, entry: MemoryEntry) -> bool:
        """Add an entry, deduplicating by lesson fingerprint.

        On duplicate the existing entry is refreshed (timestamp, verified
        upgraded from False to True) instead of appended. Returns True when a
        NEW entry was stored.
        """
        if not entry.lesson:
            return False
        entries = self.load()
        for existing in entries:
            if existing.fingerprint == entry.fingerprint:
                existing.updated_at = _now_iso()
                existing.verified = existing.verified or entry.verified
                self._save(entries)
                return False
        entries.append(entry)
        self._save(entries)
        return True

    def prune(
        self,
        entries: Optional[list[MemoryEntry]] = None,
        max_per_type: int = MAX_ENTRIES_PER_TYPE,
        max_unverified_age_days: float = MAX_UNVERIFIED_AGE_DAYS,
    ) -> list[MemoryEntry]:
        """Return the curated subset: verified + recent first, stale dropped.

        Within each error_type bucket entries are ranked verified-first, then
        newest-first, and capped at *max_per_type*. Unverified entries older
        than *max_unverified_age_days* are dropped entirely.
        """
        entries = self.load() if entries is None else entries
        kept = [
            e for e in entries
            if e.verified or e.age_days() <= max_unverified_age_days
        ]
        buckets: dict[str, list[MemoryEntry]] = {}
        for e in kept:
            buckets.setdefault(e.error_type or "unknown", []).append(e)
        result: list[MemoryEntry] = []
        for etype in sorted(buckets):
            ranked = sorted(
                buckets[etype], key=lambda e: (e.verified, e.updated_at), reverse=True
            )
            result.extend(ranked[:max_per_type])
        return result

    # ── rendering ────────────────────────────────────────────────────────

    def render_markdown(self, entries: Optional[list[MemoryEntry]] = None) -> str:
        """Render the curated entries as a compact markdown knowledge file."""
        entries = self.prune(entries)
        if not entries:
            return ""
        lines = [
            "# SyncBots curated upgrade memory",
            "",
            "Verified learnings from past LLVM/MLIR upgrade runs, grouped by "
            "error type. Entries marked [unverified] describe approaches that "
            "did NOT work -- avoid repeating them.",
            "",
        ]
        buckets: dict[str, list[MemoryEntry]] = {}
        for e in entries:
            buckets.setdefault(e.error_type or "unknown", []).append(e)
        for etype in sorted(buckets):
            lines += [f"## {etype}", ""]
            for e in buckets[etype]:
                scope_parts = []
                if e.repo:
                    scope_parts.append(e.repo)
                if e.llvm_from and e.llvm_to:
                    scope_parts.append(f"llvm {e.llvm_from[:12]} -> {e.llvm_to[:12]}")
                scope = f" ({'; '.join(scope_parts)})" if scope_parts else ""
                tag = "" if e.verified else " [unverified]"
                lines.append(f"- {e.lesson}{tag}{scope}")
            lines.append("")
        return "\n".join(lines)

    def write_rendered(self) -> str:
        """Write the curated MEMORY.md; returns its path ('' when store empty)."""
        content = self.render_markdown()
        if not content:
            return ""
        os.makedirs(self.memory_dir, exist_ok=True)
        with open(self.rendered_path, "w", encoding="utf-8") as f:
            f.write(content)
        return self.rendered_path


# ── run archiving ────────────────────────────────────────────────────────────


def _condense_summary(summary: str, limit: int = MAX_LESSON_CHARS) -> str:
    """Reduce an agent iteration summary to a single-paragraph lesson."""
    text = re.sub(r"\s+", " ", (summary or "")).strip()
    return text[:limit]


def record_run_learnings(store: MemoryStore, repo: "RepoInfo", result: "UpgradeResult") -> int:
    """Archive one upgrade run's learnings into the store.

    - On PASS: for each failure in the history, the summary of the agent
      iteration that FIXED it (the next iteration) becomes a verified entry.
      Only the last occurrence per error_type is kept.
    - On failure: the final unresolved failure becomes an unverified negative
      entry listing the approaches that did not work.

    Returns the number of NEW entries stored.
    """
    reports_by_iter = {r.iteration: r for r in result.agent_reports}
    added = 0

    if result.status == "pass":
        # Last FixAttempt per error_type; the fixing run is iteration + 1.
        latest: dict[str, object] = {}
        for attempt in result.history:
            latest[attempt.error_type] = attempt
        for etype, attempt in latest.items():
            fixing = reports_by_iter.get(attempt.iteration + 1)
            if fixing is None or not fixing.summary:
                continue
            lesson = f"Fixed {etype} during {attempt.phase}: {_condense_summary(fixing.summary)}"
            added += store.add(MemoryEntry(
                lesson=lesson, error_type=etype, repo=repo.repo_name,
                llvm_from=repo.current_llvm_hash, llvm_to=repo.target_llvm_hash,
                verified=True,
            ))
        return added

    if result.history:
        last = result.history[-1]
        tried = "; ".join(
            _condense_summary(reports_by_iter[a.iteration].summary, 120)
            for a in result.history[-3:]
            if a.iteration in reports_by_iter and reports_by_iter[a.iteration].summary
        )
        lesson = (
            f"UNRESOLVED {last.error_type} during {last.phase} "
            f"(status={result.status}). Approaches that did NOT work: {tried or 'n/a'}."
        )
        added += store.add(MemoryEntry(
            lesson=lesson, error_type=last.error_type, repo=repo.repo_name,
            llvm_from=repo.current_llvm_hash, llvm_to=repo.target_llvm_hash,
            verified=False,
        ))
    return added
