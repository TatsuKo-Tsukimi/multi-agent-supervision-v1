"""
L2 monitor hooks — disabled in v1, code path exists for v2 enable.

When supervisor.evaluate() is called, RoundExecutor passes l2_signals.
v1: always None (config.monitor_signals.audit_log/env_snapshot = False).
v2: AuditLogRecorder.collect() and EnvSnapshotCapture.collect() return real data.
"""
from __future__ import annotations

from typing import Optional, Any
from pydantic import BaseModel, Field


# ============================================================
# Data models for L2 signals (v2)
# ============================================================

class AuditLogEntry(BaseModel):
    """Service-side record of a tool call (independent of agent's claim)."""
    timestamp: float
    tool_name: str
    arguments: dict
    output: str
    caller_worker_id: int


class EnvSnapshotDiff(BaseModel):
    """Post-action state diff (e.g. file modifications, db changes)."""
    timestamp: float
    diff_type: str  # "file_change" | "db_query" | "env_var" | etc
    before: dict
    after: dict


class L2Signals(BaseModel):
    """Bundle of all L2 signals for a round. v1: always None (not collected)."""
    audit_log_entries: list[AuditLogEntry] = Field(default_factory=list)
    env_snapshots: list[EnvSnapshotDiff] = Field(default_factory=list)


# ============================================================
# Hook stubs (v1 disabled, v2 implements)
# ============================================================

class AuditLogRecorder:
    """Records service-side tool calls independently of agent's CoT claims."""

    def __init__(self, enabled: bool = False):
        self.enabled = enabled
        self._records: list[AuditLogEntry] = []

    def record(self, entry: AuditLogEntry) -> None:
        """Called by tool service layer when it receives a call. v1 stub: noop."""
        if not self.enabled:
            return
        self._records.append(entry)

    def collect(self, round_id: Optional[str] = None) -> Optional[list[AuditLogEntry]]:
        """Called by SupervisorAgent. v1 stub returns None."""
        if not self.enabled:
            return None
        # TODO v2: filter by round_id timestamp range
        return self._records.copy()

    def reset(self) -> None:
        self._records.clear()


class EnvSnapshotCapture:
    """Captures pre/post action environment state diffs."""

    def __init__(self, enabled: bool = False):
        self.enabled = enabled
        self._snapshots: list[EnvSnapshotDiff] = []

    def capture(self, diff: EnvSnapshotDiff) -> None:
        """Called by environment layer (Docker container watcher). v1 stub: noop."""
        if not self.enabled:
            return
        self._snapshots.append(diff)

    def collect(self, round_id: Optional[str] = None) -> Optional[list[EnvSnapshotDiff]]:
        """Called by SupervisorAgent. v1 stub returns None."""
        if not self.enabled:
            return None
        return self._snapshots.copy()

    def reset(self) -> None:
        self._snapshots.clear()


# ============================================================
# L2 hook bundle (instantiated per run)
# ============================================================

class L2HookBundle:
    """Bundle of all L2 hooks for one run. RoundExecutor calls collect() per round."""

    def __init__(self, audit_log_enabled: bool = False, env_snapshot_enabled: bool = False):
        self.audit_log = AuditLogRecorder(enabled=audit_log_enabled)
        self.env_snapshot = EnvSnapshotCapture(enabled=env_snapshot_enabled)

    def collect(self, round_id: Optional[str] = None) -> Optional[L2Signals]:
        """Returns None if all hooks disabled (v1 default)."""
        audit_entries = self.audit_log.collect(round_id)
        env_diffs = self.env_snapshot.collect(round_id)

        if audit_entries is None and env_diffs is None:
            return None

        return L2Signals(
            audit_log_entries=audit_entries or [],
            env_snapshots=env_diffs or [],
        )

    def reset_round(self) -> None:
        """Clear per-round buffers."""
        self.audit_log.reset()
        self.env_snapshot.reset()
