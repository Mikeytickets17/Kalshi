"""Opportunity dataclass + Sizer protocol.

This file IS the contract between Module 2 (scanner) and Module 3 (sizer).
The scanner emits Opportunity objects; the sizer consumes them and
returns an integer contract count. Any change to this dataclass is an
interface break and must bump a version string.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


INTERFACE_VERSION = "1.0.0"


@dataclass(frozen=True)
class Opportunity:
    """A structural-arb opportunity detected by the scanner.

    All prices are integer cents (1..99). All sizes are integer contracts.
    net_edge_cents is a float because fees + slippage buffer can produce
    fractional cents after rounding.
    """

    # --- identity ---
    market_ticker: str
    detected_ts_ms: int

    # --- book state ---
    yes_ask_cents: int
    yes_ask_qty: int
    no_ask_cents: int
    no_ask_qty: int

    # --- economics ---
    sum_cents: int                # yes_ask_cents + no_ask_cents
    est_fees_cents: int           # taker fees on both legs, 1 contract
    slippage_buffer_cents: int    # configured slippage reserve
    net_edge_cents: float         # 100 - sum - fees - slippage (per contract)

    # --- sizing inputs ---
    max_liquidity_contracts: int  # min(yes_ask_qty, no_ask_qty)

    # --- audit ---
    scanner_version: str = INTERFACE_VERSION

    @property
    def is_tradeable(self) -> bool:
        """Convenience: does this opp clear the minimum-profitability floor?

        Hard-coded to net_edge > 0 here; the scanner applies the
        config-driven min_edge_cents filter BEFORE emitting, so any opp
        that reaches a sizer has already passed that check. This is a
        belt-and-suspenders guard against misuse.
        """
        return self.net_edge_cents > 0 and self.max_liquidity_contracts > 0


class Sizer(Protocol):
    """Protocol the sizer module must implement.

    Input: one Opportunity + current bankroll in cents.
    Output: number of contracts to buy on EACH side (0..max_liquidity).
    Return 0 to skip the trade.

    Implementations are free to use Kelly, half-Kelly, fixed-cap, or
    hybrid strategies -- the scanner doesn't care. The only hard contract
    is: the returned integer is the count per leg, both legs get the
    same count, and 0 means 'do not trade'.
    """

    def size(self, opp: Opportunity, bankroll_cents: int) -> int: ...
