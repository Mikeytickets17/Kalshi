"""Executor (Module 3, part 2)."""

from .coid import client_order_id
from .degraded_mode import DegradedModeMonitor
from .executor import ExecutorConfig, StructuralArbExecutor
from .kalshi_api import KalshiAPI, OrderRequest, OrderResponse
from .killswitch import KillSwitch
from .live import LiveKalshiAPI
from .paper import FillModel, PaperConfig, PaperKalshiAPI
from .types import (
    OUTCOME_BOTH_FILLED,
    OUTCOME_BOTH_FILLED_IMBALANCED_UNWOUND,
    OUTCOME_BOTH_REJECTED,
    OUTCOME_HALTED_BY_LOSS_LIMIT,
    OUTCOME_KILL_SWITCH,
    OUTCOME_ONE_FILLED_UNWOUND,
    OUTCOME_UNWIND_FAILED,
    PNL_ESTIMATED_WITH_UNWIND,
    PNL_PENDING_SETTLEMENT,
    PNL_REALIZED,
    BankrollReadFailed,
    DegradedModeDetected,
    ExecutionResult,
    ExecutorError,
    KillSwitchTripped,
    LegResult,
    UnwindFailed,
)

__all__ = [
    "BankrollReadFailed",
    "DegradedModeDetected",
    "DegradedModeMonitor",
    "ExecutionResult",
    "ExecutorConfig",
    "ExecutorError",
    "FillModel",
    "KalshiAPI",
    "KillSwitch",
    "KillSwitchTripped",
    "LegResult",
    "LiveKalshiAPI",
    "OrderRequest",
    "OrderResponse",
    "OUTCOME_BOTH_FILLED",
    "OUTCOME_BOTH_FILLED_IMBALANCED_UNWOUND",
    "OUTCOME_BOTH_REJECTED",
    "OUTCOME_HALTED_BY_LOSS_LIMIT",
    "OUTCOME_KILL_SWITCH",
    "OUTCOME_ONE_FILLED_UNWOUND",
    "OUTCOME_UNWIND_FAILED",
    "PNL_ESTIMATED_WITH_UNWIND",
    "PNL_PENDING_SETTLEMENT",
    "PNL_REALIZED",
    "PaperConfig",
    "PaperKalshiAPI",
    "StructuralArbExecutor",
    "UnwindFailed",
    "client_order_id",
]
