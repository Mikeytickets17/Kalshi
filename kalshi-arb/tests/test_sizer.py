"""Sizer unit tests. Covers every Module 3 gate requirement for the sizer."""

from __future__ import annotations

import math

import pytest

from kalshi_arb.scanner.opportunity import Opportunity
from kalshi_arb.sizer import (
    BankrollSnapshot,
    HalfKellySizer,
    SizerConfig,
    SizingDecision,
)


def _opp(
    *,
    yes_ask: int = 45,
    yes_qty: int = 100,
    no_ask: int = 45,
    no_qty: int = 100,
    edge: float = 5.0,
    fees: int = 4,
) -> Opportunity:
    return Opportunity(
        market_ticker="KXTEST-1",
        detected_ts_ms=1_000_000,
        yes_ask_cents=yes_ask,
        yes_ask_qty=yes_qty,
        no_ask_cents=no_ask,
        no_ask_qty=no_qty,
        sum_cents=yes_ask + no_ask,
        est_fees_cents=fees,
        slippage_buffer_cents=0,
        net_edge_cents=edge,
        max_liquidity_contracts=min(yes_qty, no_qty),
    )


def _bank(
    *,
    cash_cents: int = 10_000_00,  # $10k
    stale: bool = False,
    daily_pnl: int = 0,
) -> BankrollSnapshot:
    return BankrollSnapshot(
        cash_cents=cash_cents,
        open_positions_value_cents=0,
        peak_equity_cents=cash_cents,
        daily_realized_pnl_cents=daily_pnl,
        taken_at_ms=1_000_000,
        stale=stale,
    )


def _sizer(**kw) -> HalfKellySizer:
    return HalfKellySizer(SizerConfig(**kw))


# ---------------------------------------------------------------------
# (1) Liquidity cap wins when it's smallest
# ---------------------------------------------------------------------


def test_max_size_from_depth() -> None:
    """Liquidity cap wins when it's smallest AND the min-profit floor is
    cleared. Use a wide edge so 3 contracts clears the $0.50 floor."""
    opp = _opp(yes_qty=3, no_qty=3, edge=20.0)  # 3 × 20c = 60c > 50c floor
    decision = _sizer(hard_cap_usd=10_000).size(opp, _bank(cash_cents=10_000_000))
    assert decision.contracts_per_leg == 3
    assert decision.liquidity_cap == 3
    assert decision.kelly_size >= 3
    assert decision.hard_cap_size >= 3


def test_min_profit_floor_blocks_tiny_liquidity_cap() -> None:
    """If liquidity is tiny AND edge is small, the floor must still block
    the trade even though liquidity would otherwise be the binding constraint."""
    opp = _opp(yes_qty=3, no_qty=3, edge=5.0)  # 3 × 5c = 15c < 50c floor
    decision = _sizer().size(opp, _bank(cash_cents=10_000_000))
    assert decision.contracts_per_leg == 0
    assert decision.min_profit_pass is False


# ---------------------------------------------------------------------
# (2) Half-Kelly matches hand calculation at 5 deterministic points
# ---------------------------------------------------------------------


HALF_KELLY_POINTS = [
    # (edge_cents, yes_ask, no_ask, cash_cents, expected_kelly_contracts)
    # kelly_fraction = 0.5 * edge/100 * cash / cost; floor.
    # 1. edge=5c, cost=90c, cash=$1000: 0.5*0.05*100000/90 = 27.77 -> 27
    (5.0, 45, 45, 100_000, 27),
    # 2. edge=10c, cost=80c, cash=$1000: 0.5*0.10*100000/80 = 62.5 -> 62
    (10.0, 40, 40, 100_000, 62),
    # 3. edge=1c, cost=95c, cash=$1000: 0.5*0.01*100000/95 = 5.26 -> 5
    (1.0, 50, 45, 100_000, 5),
    # 4. edge=50c, cost=50c, cash=$1000: 0.5*0.50*100000/50 = 500 -> 500
    (50.0, 25, 25, 100_000, 500),
    # 5. edge=2c, cost=98c, cash=$100: 0.5*0.02*10000/98 = 1.02 -> 1
    (2.0, 50, 48, 10_000, 1),
]


@pytest.mark.parametrize("edge,yes_ask,no_ask,cash,expected", HALF_KELLY_POINTS)
def test_half_kelly_hand_calculation(
    edge: float, yes_ask: int, no_ask: int, cash: int, expected: int
) -> None:
    opp = _opp(yes_ask=yes_ask, no_ask=no_ask, yes_qty=100_000, no_qty=100_000, edge=edge)
    # Set hard_cap absurdly high so kelly is the binding constraint.
    decision = _sizer(hard_cap_usd=1_000_000).size(opp, _bank(cash_cents=cash))
    assert decision.kelly_size == expected


# ---------------------------------------------------------------------
# (3) Hard cap wins when it's smallest
# ---------------------------------------------------------------------


def test_hard_cap_wins_over_kelly() -> None:
    # Big cash, generous book, tiny hard cap.
    opp = _opp(yes_ask=40, no_ask=40, yes_qty=100_000, no_qty=100_000, edge=10.0)
    decision = _sizer(hard_cap_usd=20.0).size(opp, _bank(cash_cents=100_000_000))
    # hard_cap = $20 / $0.80 = 25 contracts.
    assert decision.hard_cap_size == 25
    assert decision.contracts_per_leg == 25  # hard cap is smallest


# ---------------------------------------------------------------------
# (4) Combined min() rule actually fires for each input
# ---------------------------------------------------------------------


def test_combined_min_rule_runs() -> None:
    # Case A: liquidity is smallest.
    a = _sizer(hard_cap_usd=1_000).size(
        _opp(yes_qty=2, no_qty=2, edge=50.0),
        _bank(cash_cents=10_000_000),
    )
    assert a.contracts_per_leg == 2

    # Case B: kelly is smallest. Cash very low; small edge.
    b = _sizer(hard_cap_usd=10_000).size(
        _opp(yes_qty=100_000, no_qty=100_000, edge=1.0),
        _bank(cash_cents=10_000),  # $100
    )
    assert b.contracts_per_leg == b.kelly_size
    assert b.contracts_per_leg <= b.liquidity_cap
    assert b.contracts_per_leg <= b.hard_cap_size

    # Case C: hard cap is smallest.
    c = _sizer(hard_cap_usd=5.0).size(
        _opp(yes_qty=100_000, no_qty=100_000, edge=50.0),
        _bank(cash_cents=10_000_000),
    )
    assert c.contracts_per_leg == c.hard_cap_size


# ---------------------------------------------------------------------
# (5) Min-profit floor rejects low-dollar trades
# ---------------------------------------------------------------------


def test_min_profit_floor_rejects_low_dollar_trades() -> None:
    # 1 contract × 1c edge = 1c expected profit. Floor is 50c.
    # Make sure liquidity/cash force final_size=1.
    opp = _opp(yes_qty=1, no_qty=1, edge=1.0)
    decision = _sizer(
        hard_cap_usd=1000, min_expected_profit_usd=0.50
    ).size(opp, _bank(cash_cents=10_000_000))
    assert decision.contracts_per_leg == 0
    assert decision.min_profit_pass is False
    assert "expected profit" in decision.reason


def test_min_profit_floor_allows_sufficient_profit() -> None:
    # 100 contracts × 1c = $1.00 expected profit >= 0.50 floor.
    opp = _opp(yes_qty=1000, no_qty=1000, edge=1.0)
    decision = _sizer(
        hard_cap_usd=1000, min_expected_profit_usd=0.50
    ).size(opp, _bank(cash_cents=10_000_000))
    assert decision.contracts_per_leg > 0
    assert decision.min_profit_pass is True


# ---------------------------------------------------------------------
# (6) Zero size when bankroll too low
# ---------------------------------------------------------------------


def test_zero_size_when_bankroll_too_low() -> None:
    opp = _opp(yes_ask=50, no_ask=45)  # cost = 95c
    # $0.50 cash < 1 contract cost ($0.95).
    decision = _sizer().size(opp, _bank(cash_cents=50))
    assert decision.contracts_per_leg == 0


# ---------------------------------------------------------------------
# (7) Stale snapshot refuses immediately
# ---------------------------------------------------------------------


def test_stale_snapshot_rejects_immediately() -> None:
    opp = _opp()
    decision = _sizer().size(opp, _bank(stale=True))
    assert decision.contracts_per_leg == 0
    assert "stale" in decision.reason.lower()
    # Verify early-return: kelly_size / hard_cap_size not even computed.
    assert decision.kelly_size == 0


# ---------------------------------------------------------------------
# (8) Drawdown already past limit rejects
# ---------------------------------------------------------------------


def test_drawdown_already_past_limit_rejects() -> None:
    opp = _opp()
    decision = _sizer(daily_loss_limit_usd=500.0).size(
        opp, _bank(daily_pnl=-50_001)  # -$500.01
    )
    assert decision.contracts_per_leg == 0
    assert "daily" in decision.reason.lower()
