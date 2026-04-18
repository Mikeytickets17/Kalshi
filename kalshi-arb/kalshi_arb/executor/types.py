"""Executor types. All fields part of the public interface -- see
sizer/EXECUTOR_INTERFACE.md."""

from __future__ import annotations

from dataclasses import dataclass

from ..sizer.types import SizingDecision


# Outcome labels.
OUTCOME_BOTH_FILLED = "both_filled"
OUTCOME_BOTH_FILLED_IMBALANCED_UNWOUND = "both_filled_imbalanced_unwound"
OUTCOME_ONE_FILLED_UNWOUND = "one_filled_unwound"
OUTCOME_BOTH_REJECTED = "both_rejected"
OUTCOME_KILL_SWITCH = "kill_switch"
OUTCOME_HALTED_BY_LOSS_LIMIT = "halted_by_loss_limit"
OUTCOME_UNWIND_FAILED = "unwind_failed"

# P&L confidence labels (see EXECUTOR_INTERFACE.md).
PNL_REALIZED = "realized"
PNL_ESTIMATED_WITH_UNWIND = "estimated_with_unwind"
PNL_PENDING_SETTLEMENT = "pending_settlement"


@dataclass(frozen=True)
class LegResult:
    side: str                      # 'yes' | 'no'
    action: str                    # 'buy' | 'sell' (sell only on unwind)
    limit_cents: int               # 0 for market orders
    requested_count: int
    filled_count: int
    kalshi_order_id: str | None
    client_order_id: str
    placed_ts_ms: int
    first_response_ts_ms: int | None
    error: str | None = None


@dataclass(frozen=True)
class ExecutionResult:
    decision: SizingDecision
    fired_ts_ms: int
    legs: tuple[LegResult, ...]
    outcome: str
    net_fill_cents: int | None
    total_fees_cents: int
    pnl_confidence: str
    error: str | None = None


# Error hierarchy.
class ExecutorError(Exception):
    """Base for all executor-initiated errors."""


class KillSwitchTripped(ExecutorError):
    """Kill switch was set before or during an execute() call."""


class UnwindFailed(ExecutorError):
    """An unwind market order did not fill within the timeout. CRITICAL."""


class DegradedModeDetected(ExecutorError):
    """Sequential portfolio reads disagreed with no execution between."""


class BankrollReadFailed(ExecutorError):
    """Could not read balance or positions from the exchange."""
