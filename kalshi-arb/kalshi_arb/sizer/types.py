"""Sizer types. Every field here is part of the public interface --
see sizer/EXECUTOR_INTERFACE.md. Interface-shape test guards the schema.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..scanner.opportunity import Opportunity


INTERFACE_VERSION = "1.0.0"


@dataclass(frozen=True)
class BankrollSnapshot:
    cash_cents: int
    open_positions_value_cents: int
    peak_equity_cents: int
    daily_realized_pnl_cents: int
    taken_at_ms: int
    stale: bool


@dataclass(frozen=True)
class SizingDecision:
    """What the sizer returns to the executor.

    contracts_per_leg == 0 means skip this trade. The executor MUST still
    record the decision for audit even when skipping.
    """

    opportunity: Opportunity
    contracts_per_leg: int
    reason: str
    liquidity_cap: int
    kelly_size: int
    hard_cap_size: int
    min_profit_pass: bool
    bankroll_snapshot: BankrollSnapshot
    sizer_version: str = INTERFACE_VERSION
