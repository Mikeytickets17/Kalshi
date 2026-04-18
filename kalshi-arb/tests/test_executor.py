"""Executor unit tests. Covers every Module 3 gate requirement for the
executor, plus the idempotency and pnl_confidence additions from review."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from kalshi_arb.executor import (
    DegradedModeMonitor,
    ExecutorConfig,
    KillSwitch,
    OUTCOME_BOTH_FILLED,
    OUTCOME_BOTH_FILLED_IMBALANCED_UNWOUND,
    OUTCOME_BOTH_REJECTED,
    OUTCOME_HALTED_BY_LOSS_LIMIT,
    OUTCOME_KILL_SWITCH,
    OUTCOME_ONE_FILLED_UNWOUND,
    OUTCOME_UNWIND_FAILED,
    PNL_ESTIMATED_WITH_UNWIND,
    PNL_REALIZED,
    StructuralArbExecutor,
    client_order_id,
)
from kalshi_arb.executor.kalshi_api import PortfolioSnapshot
from kalshi_arb.scanner.opportunity import Opportunity
from kalshi_arb.sizer import BankrollSnapshot, SizingDecision

from tests.fakes import (
    FakeKalshiAPI,
    policy_both_fill_fully,
    policy_both_reject,
    policy_partial_imbalance,
    policy_unwind_never_fills,
    policy_yes_fills_no_rejects,
)


# -------- Shared fixtures / helpers -----------------------------------


def _opp(
    *,
    yes_ask: int = 40,
    no_ask: int = 50,
    yes_qty: int = 100,
    no_qty: int = 100,
    edge: float = 5.5,
    detected_ts_ms: int = 1_700_000_000_000,
    ticker: str = "KXTEST-1",
) -> Opportunity:
    return Opportunity(
        market_ticker=ticker,
        detected_ts_ms=detected_ts_ms,
        yes_ask_cents=yes_ask,
        yes_ask_qty=yes_qty,
        no_ask_cents=no_ask,
        no_ask_qty=no_qty,
        sum_cents=yes_ask + no_ask,
        est_fees_cents=4,
        slippage_buffer_cents=0,
        net_edge_cents=edge,
        max_liquidity_contracts=min(yes_qty, no_qty),
    )


def _decision(opp: Opportunity, contracts: int = 10) -> SizingDecision:
    return SizingDecision(
        opportunity=opp,
        contracts_per_leg=contracts,
        reason="test",
        liquidity_cap=min(opp.yes_ask_qty, opp.no_ask_qty),
        kelly_size=contracts,
        hard_cap_size=contracts,
        min_profit_pass=True,
        bankroll_snapshot=BankrollSnapshot(
            cash_cents=100_00000,
            open_positions_value_cents=0,
            peak_equity_cents=100_00000,
            daily_realized_pnl_cents=0,
            taken_at_ms=0,
            stale=False,
        ),
    )


@pytest.fixture
def killswitch(tmp_path: Path) -> KillSwitch:
    return KillSwitch(sentinel=tmp_path / "KILL_SWITCH")


def _make_executor(
    api: FakeKalshiAPI,
    killswitch: KillSwitch,
    *,
    daily_loss_limit_cents: int = 50_000,
    unwind_timeout_sec: float = 5.0,
    critical_unwind_dir: Path | None = None,
) -> StructuralArbExecutor:
    return StructuralArbExecutor(
        api=api,
        killswitch=killswitch,
        config=ExecutorConfig(
            daily_loss_limit_cents=daily_loss_limit_cents,
            unwind_timeout_sec=unwind_timeout_sec,
            critical_unwind_dir=critical_unwind_dir or killswitch.sentinel.parent,
        ),
    )


# -------- (1) Parallel IOC dispatch -----------------------------------


def test_parallel_ioc_dispatch(killswitch: KillSwitch) -> None:
    """asyncio.gather must fire both legs concurrently -- wall-clock should
    be ~place_delay_sec, NOT 2 × place_delay_sec."""
    api = FakeKalshiAPI(fill_policy=policy_both_fill_fully, place_delay_sec=0.5)
    executor = _make_executor(api, killswitch)
    decision = _decision(_opp())

    t0 = time.monotonic()
    result = asyncio.run(executor.execute(decision))
    elapsed = time.monotonic() - t0

    assert result.outcome == OUTCOME_BOTH_FILLED
    # Sequential would be ~1.0s; parallel should be <0.75s.
    assert elapsed < 0.8, f"legs ran sequentially; elapsed={elapsed:.2f}s"
    assert len(api.placed_orders) == 2


# -------- (12) Clean arb reports realized -----------------------------


def test_clean_arb_reports_realized_confidence(killswitch: KillSwitch) -> None:
    api = FakeKalshiAPI(fill_policy=policy_both_fill_fully)
    executor = _make_executor(api, killswitch)
    result = asyncio.run(executor.execute(_decision(_opp())))
    assert result.outcome == OUTCOME_BOTH_FILLED
    assert result.pnl_confidence == PNL_REALIZED


# -------- (2) Unwind on half-fill (only one leg fills) ----------------


def test_unwind_on_half_fill(killswitch: KillSwitch) -> None:
    api = FakeKalshiAPI(fill_policy=policy_yes_fills_no_rejects)
    executor = _make_executor(api, killswitch)

    result = asyncio.run(executor.execute(_decision(_opp(), contracts=10)))

    assert result.outcome == OUTCOME_ONE_FILLED_UNWOUND
    assert result.pnl_confidence == PNL_ESTIMATED_WITH_UNWIND
    # Three legs: yes (filled buy), no (rejected buy), unwind-yes (market sell).
    assert len(result.legs) == 3
    unwind = result.legs[2]
    assert unwind.action == "sell"
    assert unwind.side == "yes"
    assert unwind.requested_count == 10
    assert unwind.filled_count == 10


# -------- (3) Unwind on imbalance only --------------------------------


def test_unwind_on_imbalance_only(killswitch: KillSwitch) -> None:
    """Both legs fill but with different quantities -- unwind only the
    IMBALANCE (review Q4). YES=8/10, NO=10/10 -> unwind 2 YES."""
    api = FakeKalshiAPI(fill_policy=policy_partial_imbalance(yes_fill=8, no_fill=10))
    executor = _make_executor(api, killswitch)

    result = asyncio.run(executor.execute(_decision(_opp(), contracts=10)))

    assert result.outcome == OUTCOME_BOTH_FILLED_IMBALANCED_UNWOUND
    assert result.pnl_confidence == PNL_ESTIMATED_WITH_UNWIND
    assert len(result.legs) == 3
    unwind = result.legs[2]
    # NO had more fills; unwind NO by the difference (10-8=2).
    assert unwind.side == "no"
    assert unwind.requested_count == 2
    assert unwind.filled_count == 2


# -------- (4) Unwind at market on leg failure -------------------------


def test_unwind_at_market_on_leg_failure(killswitch: KillSwitch) -> None:
    api = FakeKalshiAPI(fill_policy=policy_yes_fills_no_rejects)
    executor = _make_executor(api, killswitch)
    result = asyncio.run(executor.execute(_decision(_opp(), contracts=5)))

    assert result.outcome == OUTCOME_ONE_FILLED_UNWOUND
    assert result.pnl_confidence == PNL_ESTIMATED_WITH_UNWIND
    unwind = result.legs[2]
    assert unwind.action == "sell"
    assert unwind.limit_cents == 0   # market order


# -------- (5) Unwind failure -> sentinel + kill switch + UnwindFailed --


def test_unwind_failure_writes_sentinel_and_trips_killswitch(
    killswitch: KillSwitch, tmp_path: Path
) -> None:
    api = FakeKalshiAPI(fill_policy=policy_unwind_never_fills)
    executor = _make_executor(
        api, killswitch, unwind_timeout_sec=1.0, critical_unwind_dir=tmp_path
    )

    result = asyncio.run(executor.execute(_decision(_opp(), contracts=5)))

    assert result.outcome == OUTCOME_UNWIND_FAILED
    # Kill switch file exists.
    assert killswitch.is_tripped()
    # Sentinel file exists.
    sentinels = list(tmp_path.glob("CRITICAL_UNWIND_FAILED_*.txt"))
    assert len(sentinels) == 1
    body = sentinels[0].read_text()
    assert "TICKER: KXTEST-1" in body
    assert "SIDE: yes" in body
    assert "OUTSTANDING_CONTRACTS: 5" in body


# -------- (6) Kill switch short-circuits ------------------------------


def test_kill_switch_short_circuits(killswitch: KillSwitch) -> None:
    killswitch.trip("manual")
    api = FakeKalshiAPI(fill_policy=policy_both_fill_fully)
    executor = _make_executor(api, killswitch)

    result = asyncio.run(executor.execute(_decision(_opp())))

    assert result.outcome == OUTCOME_KILL_SWITCH
    assert len(api.placed_orders) == 0  # no orders fired


# -------- (7) Daily loss limit auto-trip ------------------------------


def test_daily_loss_limit_auto_trip(killswitch: KillSwitch) -> None:
    """Push realized P&L past threshold; the next execute() must report
    halted_by_loss_limit."""
    # Fill policy that produces negative realized P&L:
    # We buy at 40+50=90c, and because structural arb with no edge means
    # settle at 100c (1 contract pays $1). That's +10c per contract gross
    # minus fees -- positive. To simulate a loss, give a policy where our
    # fills happen ABOVE their limit (shouldn't happen in IOC but we want
    # to force the counter). Simpler: directly push the counter via an
    # initial "losing" trade using a policy that produces negative net.
    #
    # Cleaner: use the sum_ge_100 anti-arb case. yes=55 + no=60 = 115c,
    # we pay 115c, settle pays 100c, loss = 15c per contract. Then fill
    # a lot of them until daily loss crosses.

    api = FakeKalshiAPI(fill_policy=policy_both_fill_fully)
    # loss_limit = $100 for quick test. Trade 10 contracts at 115c sum =
    # 10 × (115 - 100) = 150c loss per pair -- under the limit. Keep firing.
    executor = _make_executor(api, killswitch, daily_loss_limit_cents=50_00)  # $50

    losing = _opp(yes_ask=60, no_ask=55, edge=0.0)  # sum=115c, loses 15c/pair
    # First execute: lose ~150c on 10 contracts -> still under $50 limit.
    r1 = asyncio.run(executor.execute(_decision(losing, contracts=10)))
    assert r1.outcome == OUTCOME_BOTH_FILLED
    assert executor.daily_realized_pnl_cents < 0

    # Keep firing until we cross -$50.
    while (
        not killswitch.is_tripped()
        and executor.daily_realized_pnl_cents > -50_00
    ):
        asyncio.run(executor.execute(_decision(losing, contracts=10)))

    # Now the next execute should be halted.
    r_last = asyncio.run(executor.execute(_decision(losing, contracts=10)))
    assert r_last.outcome in (OUTCOME_HALTED_BY_LOSS_LIMIT, OUTCOME_KILL_SWITCH)


# -------- (8) Estimated unwind doesn't count toward daily limit -------


def test_estimated_unwind_does_not_count_toward_daily_limit(
    killswitch: KillSwitch,
) -> None:
    """Chained unwinds with hypothetical large estimated losses must NOT
    trip the daily loss limit -- only 'realized' counts."""
    api = FakeKalshiAPI(fill_policy=policy_yes_fills_no_rejects)
    executor = _make_executor(api, killswitch, daily_loss_limit_cents=100)  # $1

    # Fire many half-fill trades (each results in unwind, confidence=estimated).
    for _ in range(20):
        result = asyncio.run(executor.execute(_decision(_opp(), contracts=5)))
        assert result.pnl_confidence == PNL_ESTIMATED_WITH_UNWIND

    # Even with 20 unwinds, the realized counter is untouched, kill switch not tripped.
    assert executor.daily_realized_pnl_cents == 0
    assert not killswitch.is_tripped()


# -------- (9) Degraded mode trips kill switch -------------------------


def test_degraded_mode_trips_kill_switch(killswitch: KillSwitch) -> None:
    monitor = DegradedModeMonitor(killswitch=killswitch)
    # Read 1: cash $100, no positions.
    monitor.record_read(cash_cents=10000, positions={}, at_ms=1000)
    # Read 2 shortly after: cash changed, no execution between.
    monitor.record_read(cash_cents=9000, positions={}, at_ms=1100)
    assert monitor.tripped
    assert killswitch.is_tripped()
    assert "degraded_mode" in killswitch.sentinel.read_text()


def test_degraded_mode_allows_changes_after_execution(killswitch: KillSwitch) -> None:
    monitor = DegradedModeMonitor(killswitch=killswitch)
    monitor.record_read(cash_cents=10000, positions={}, at_ms=1000)
    monitor.record_execution(at_ms=1050)  # we fired an order here
    monitor.record_read(cash_cents=9000, positions={"KXTEST-1": 5}, at_ms=1100)
    assert not monitor.tripped
    assert not killswitch.is_tripped()


# -------- (10) client_order_id determinism ---------------------------


def test_client_order_id_is_deterministic() -> None:
    coid1 = client_order_id("KXBTC-1", 1_000_000, "yes", "arb")
    coid2 = client_order_id("KXBTC-1", 1_000_000, "yes", "arb")
    assert coid1 == coid2
    # Any input changes -> ID changes.
    assert coid1 != client_order_id("KXBTC-2", 1_000_000, "yes", "arb")
    assert coid1 != client_order_id("KXBTC-1", 1_000_001, "yes", "arb")
    assert coid1 != client_order_id("KXBTC-1", 1_000_000, "no", "arb")
    assert coid1 != client_order_id("KXBTC-1", 1_000_000, "yes", "unwind")
    # Format check: deterministic length, prefix.
    assert coid1.startswith("kac_")
    assert len(coid1) == 32


# -------- (11) Idempotency end-to-end ---------------------------------


def test_double_execute_same_decision_produces_one_order(killswitch: KillSwitch) -> None:
    """Calling execute() twice with the same decision must NOT place two
    distinct orders at Kalshi. FakeKalshiAPI dedupes by client_order_id
    just like real Kalshi does."""
    api = FakeKalshiAPI(fill_policy=policy_both_fill_fully)
    executor = _make_executor(api, killswitch)
    decision = _decision(_opp(), contracts=5)

    r1 = asyncio.run(executor.execute(decision))
    r2 = asyncio.run(executor.execute(decision))

    # Both calls saw the full fill response.
    assert r1.outcome == OUTCOME_BOTH_FILLED
    assert r2.outcome == OUTCOME_BOTH_FILLED

    # Collect client_order_ids placed. Every distinct COID = one real order.
    placed_coids = {req.client_order_id for req in api.placed_orders}
    # Two COIDs expected (yes + no), NOT four.
    assert len(placed_coids) == 2, (
        f"Expected 2 unique client_order_ids (yes+no); got {len(placed_coids)}. "
        f"Real Kalshi dedupes by COID; idempotency is broken."
    )
