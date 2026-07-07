"""In-memory run store for the fire-and-forget pipeline.

The /chat endpoint creates a run entry and spawns the pipeline as a
background task.  The /polling endpoint reads from this store to return
live status, events (thoughts, version tiles), and the final workspace
snapshot.

Entries are kept for 30 minutes after completion, then lazily evicted.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RunEntry:
    run_id: str
    workspace_id: str
    status: str = "running"           # "running" | "completed" | "error"
    events: list[dict] = field(default_factory=list)
    workspace: dict | None = None     # serialised workspace, set on completion
    error_message: str | None = None
    created_at: float = field(default_factory=time.time)
    completed_at: float | None = None

    TTL_SECONDS: int = 1800           # 30 min


class RunStore:
    """Thread-safe (asyncio-safe) in-memory store for agent runs."""

    def __init__(self) -> None:
        self._runs: dict[str, RunEntry] = {}

    # ------------------------------------------------------------------
    # Write API (called by the background task / graph)
    # ------------------------------------------------------------------

    def create(self, run_id: str, workspace_id: str) -> RunEntry:
        entry = RunEntry(run_id=run_id, workspace_id=workspace_id)
        self._runs[run_id] = entry
        return entry

    def push_event(self, run_id: str, event: dict) -> None:
        entry = self._runs.get(run_id)
        if entry:
            entry.events.append(event)

    def complete(self, run_id: str, workspace: dict) -> None:
        entry = self._runs.get(run_id)
        if entry:
            entry.status = "completed"
            entry.workspace = workspace
            entry.completed_at = time.time()

    def fail(self, run_id: str, error_message: str) -> None:
        entry = self._runs.get(run_id)
        if entry:
            entry.status = "error"
            entry.error_message = error_message
            entry.completed_at = time.time()

    # ------------------------------------------------------------------
    # Read API (called by /polling endpoint)
    # ------------------------------------------------------------------

    def get(self, run_id: str) -> RunEntry | None:
        self._evict()
        return self._runs.get(run_id)

    def snapshot(self, run_id: str) -> dict[str, Any]:
        """Return a JSON-serialisable poll response."""
        entry = self.get(run_id)
        if entry is None:
            return {"status": "not_found", "events": [], "workspace": None}
        return {
            "status": entry.status,
            "events": entry.events,
            "workspace": entry.workspace,
            "error": entry.error_message,
        }

    # ------------------------------------------------------------------
    # Eviction
    # ------------------------------------------------------------------

    def _evict(self) -> None:
        now = time.time()
        stale = [
            rid for rid, e in self._runs.items()
            if e.completed_at and (now - e.completed_at) > RunEntry.TTL_SECONDS
        ]
        for rid in stale:
            del self._runs[rid]


# Singleton — shared across the process
run_store = RunStore()
