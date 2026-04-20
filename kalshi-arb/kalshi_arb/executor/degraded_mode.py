"""Degraded-mode detector.

Two sequential portfolio reads whose positions OR cash balance differ,
WITH NO ExecutionResult recorded between them, indicates something is
wrong: stale API response, phantom fill, or a competing process on the
same account.

Review Q2 answered: exact match required, no dust tolerance.

When a disagreement is observed, trip the kill switch and raise
DegradedModeDetected at the next execute() attempt. The monitor does
not kill the process -- it flips a flag that the executor checks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .. import log
from .killswitch import KillSwitch

_log = log.get("executor.degraded_mode")


@dataclass
class _Read:
    cash_cents: int
    positions: dict[str, int]  # ticker -> signed contract count
    at_ms: int


@dataclass
class DegradedModeMonitor:
    """Compares each new portfolio read against the previous. If anything
    changed and no execution was recorded between, trips the kill switch."""

    killswitch: KillSwitch
    _last_read: _Read | None = None
    _last_execution_ms: int = 0
    _tripped: bool = False

    def record_execution(self, at_ms: int) -> None:
        """Called by the executor after every ExecutionResult. Any portfolio
        change seen after this point is explainable by our own activity."""
        self._last_execution_ms = max(self._last_execution_ms, at_ms)

    def record_read(
        self, *, cash_cents: int, positions: dict[str, int], at_ms: int
    ) -> None:
        read = _Read(cash_cents=cash_cents, positions=dict(positions), at_ms=at_ms)
        prev = self._last_read
        self._last_read = read
        if prev is None:
            return
        # Was there an execution between the two reads? If yes, differences
        # are expected and fine.
        if prev.at_ms < self._last_execution_ms <= read.at_ms:
            return
        if prev.cash_cents != read.cash_cents or prev.positions != read.positions:
            self._tripped = True
            reason = (
                f"degraded_mode_inconsistent_reads: "
                f"cash {prev.cash_cents}->{read.cash_cents}, "
                f"pos_changed={prev.positions != read.positions}, "
                f"gap={read.at_ms - prev.at_ms}ms, "
                f"last_exec={self._last_execution_ms}ms"
            )
            _log.critical("degraded_mode.detected", reason=reason)
            self.killswitch.trip(reason)

    @property
    def tripped(self) -> bool:
        return self._tripped
