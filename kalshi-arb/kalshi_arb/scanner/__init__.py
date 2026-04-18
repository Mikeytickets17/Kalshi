"""Structural arbitrage scanner (Module 2)."""

from .book import BookState, OrderBook
from .fees import FeeModel, FeeTier
from .opportunity import INTERFACE_VERSION, Opportunity, Sizer
from .scanner import (
    DECISION_EMIT,
    DECISION_SKIP_BELOW_EDGE,
    DECISION_SKIP_EMPTY,
    DECISION_SKIP_HALTED,
    DECISION_SKIP_SUM_GE_100,
    ScanDecision,
    ScannerConfig,
    StructuralArbScanner,
)

__all__ = [
    "BookState",
    "DECISION_EMIT",
    "DECISION_SKIP_BELOW_EDGE",
    "DECISION_SKIP_EMPTY",
    "DECISION_SKIP_HALTED",
    "DECISION_SKIP_SUM_GE_100",
    "FeeModel",
    "FeeTier",
    "INTERFACE_VERSION",
    "Opportunity",
    "OrderBook",
    "ScanDecision",
    "ScannerConfig",
    "Sizer",
    "StructuralArbScanner",
]
