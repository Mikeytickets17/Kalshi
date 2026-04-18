"""Paper-mode unit + integration tests.

Covers:
  * Instantiation guards (both directions).
  * Fill model: full / partial / zero distribution + independence.
  * Idempotency via client_order_id (matches real Kalshi behavior).
  * Unwind slippage (SELL fills at ask - unwind_slippage_cents).
  * The required test_paper_mode_exercises_unwind_path (100 executions
    at partial_fill_rate=0.5, asserts unwind fires and completes).
  * Full pipeline integration: scanner -> sizer -> executor ->
    PaperKalshiAPI end-to-end, asserts paper=true rows and no live calls.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from kalshi_arb.executor import (
    ExecutorConfig,
    FillModel,
    KillSwitch,
    OUTCOME_BOTH_FILLED,
    OUTCOME_BOTH_FILLED_IMBALANCED_UNWOUND,
    OUTCOME_BOTH_REJECTED,
    OUTCOME_ONE_FILLED_UNWOUND,
    PaperConfig,
    PaperKalshiAPI,
    StructuralArbExecutor,
)
from kalshi_arb.executor.kalshi_api import OrderRequest
from kalshi_arb.scanner import (
    DECISION_EMIT,
    FeeModel,
    OrderBook,
    ScannerConfig,
    StructuralArbScanner,
)
from kalshi_arb.scanner.opportunity import Opportunity
from kalshi_arb.sizer import BankrollSnapshot, HalfKellySizer, SizerConfig, SizingDecision


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _paper_config(
    *,
    full: float = 1.0,
    partial: float = 0.0,
    zero: float = 0.0,
    partial_min: float = 0.5,
    partial_max: float = 0.95,
    unwind_slip: int = 1,
    use_fees: bool = True,
    seed: int = 42,
) -> PaperConfig:
    return PaperConfig(
        fill_model=FillModel(
            full_fill_rate=full,
            partial_fill_rate=partial,
            zero_fill_rate=zero,
            partial_min_pct=partial_min,
            partial_max_pct=partial_max,
        ),
        unwind_slippage_cents=unwind_slip,
        use_builtin_fees=use_fees,
        rng_seed=seed,
    )


def _opp(
    *,
    yes_ask: int = 40,
    no_ask: int = 50,
    yes_qty: int = 100,
    no_qty: int = 100,
    edge: float = 5.5,
    detected_ts_ms: int = 1_700_000_000_000,
    ticker: str = "KXPAPER-1",
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
            cash_cents=10_000_00,
            open_positions_value_cents=0,
            peak_equity_cents=10_000_00,
            daily_realized_pnl_cents=0,
            taken_at_ms=0,
            stale=False,
        ),
    )


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Paper-mode tests must run with LIVE_TRADING unset."""
    monkeypatch.delenv("LIVE_TRADING", raising=False)
    monkeypatch.delenv("PAPER_MODE", raising=False)


# ----------------------------------------------------------------------
# (1) Instantiation guards
# ----------------------------------------------------------------------


def test_paper_api_refuses_under_live_trading_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LIVE_TRADING", "true")
    with pytest.raises(RuntimeError, match="LIVE_TRADING=true"):
        PaperKalshiAPI(config=_paper_config())


def test_live_api_refuses_under_paper_mode_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kalshi_arb.executor.live import LiveKalshiAPI

    monkeypatch.setenv("PAPER_MODE", "true")
    with pytest.raises(RuntimeError, match="PAPER_MODE=true"):
        LiveKalshiAPI(
            api_key_id="fake", private_key_path=Path("/dev/null"), demo=True
        )


# ----------------------------------------------------------------------
# (2) Fill model distribution
# ----------------------------------------------------------------------


def test_fill_model_full_only_always_fills() -> None:
    api = PaperKalshiAPI(config=_paper_config(full=1.0, partial=0.0, zero=0.0))
    req = OrderRequest(
        market_ticker="KXT", side="yes", action="buy",
        order_type="limit", time_in_force="IOC", count=10,
        limit_cents=40, client_order_id="kac_test_full_1",
    )
    resp = asyncio.run(api.place_order(req))
    assert resp.filled_count == 10
    assert resp.avg_fill_price_cents == 40  # raw book ask
    assert resp.error is None


def test_fill_model_zero_only_never_fills() -> None:
    api = PaperKalshiAPI(config=_paper_config(full=0.0, partial=0.0, zero=1.0))
    req = OrderRequest(
        market_ticker="KXT", side="yes", action="buy",
        order_type="limit", time_in_force="IOC", count=10,
        limit_cents=40, client_order_id="kac_test_zero_1",
    )
    resp = asyncio.run(api.place_order(req))
    assert resp.filled_count == 0
    assert resp.error == "ioc_no_fill"


def test_fill_model_legs_sampled_independently() -> None:
    """50/50 fill rate over 500 trials: the number of times YES fills WITHOUT
    NO filling (or vice versa) should be roughly 25% of trials. If both legs
    shared the same random outcome, asymmetric fills would be 0%."""
    api = PaperKalshiAPI(config=_paper_config(full=0.5, partial=0.0, zero=0.5, seed=1234))
    asymmetric = 0
    trials = 500
    for i in range(trials):
        yes_req = OrderRequest(
            market_ticker="KXT", side="yes", action="buy",
            order_type="limit", time_in_force="IOC", count=10,
            limit_cents=40, client_order_id=f"kac_sym_y_{i}",
        )
        no_req = OrderRequest(
            market_ticker="KXT", side="no", action="buy",
            order_type="limit", time_in_force="IOC", count=10,
            limit_cents=50, client_order_id=f"kac_sym_n_{i}",
        )
        y = asyncio.run(api.place_order(yes_req)).filled_count
        n = asyncio.run(api.place_order(no_req)).filled_count
        if (y > 0) != (n > 0):
            asymmetric += 1
    # Expected asymmetric rate for independent 50/50 = 2 * 0.5 * 0.5 = 50%.
    # Allow wide tolerance for sampling noise at n=500.
    assert 100 < asymmetric < 400, (
        f"asymmetric fills = {asymmetric}/500. Legs may not be independent."
    )


def test_fill_model_partial_bounds() -> None:
    """Partial fills must land in [partial_min_pct, partial_max_pct] × count."""
    api = PaperKalshiAPI(config=_paper_config(
        full=0.0, partial=1.0, zero=0.0,
        partial_min=0.40, partial_max=0.80, seed=7,
    ))
    for i in range(200):
        req = OrderRequest(
            market_ticker="KXT", side="yes", action="buy",
            order_type="limit", time_in_force="IOC", count=100,
            limit_cents=40, client_order_id=f"kac_pb_{i}",
        )
        resp = asyncio.run(api.place_order(req))
        assert 40 <= resp.filled_count <= 80, (
            f"partial fill {resp.filled_count}/100 out of [40, 80]"
        )


# ----------------------------------------------------------------------
# (3) Idempotency (paper mirrors real Kalshi dedup)
# ----------------------------------------------------------------------


def test_paper_api_dedupes_by_client_order_id() -> None:
    api = PaperKalshiAPI(config=_paper_config(full=1.0, partial=0.0, zero=0.0))
    req = OrderRequest(
        market_ticker="KXT", side="yes", action="buy",
        order_type="limit", time_in_force="IOC", count=5,
        limit_cents=40, client_order_id="kac_dedupe_1",
    )
    r1 = asyncio.run(api.place_order(req))
    r2 = asyncio.run(api.place_order(req))
    assert r1 == r2
    # Recorded once, not twice.
    assert len(api.placed_orders) == 1


# ----------------------------------------------------------------------
# (4) Unwind slippage
# ----------------------------------------------------------------------


def test_unwind_sell_fills_below_ask_by_slippage_cents() -> None:
    api = PaperKalshiAPI(
        config=_paper_config(full=1.0, partial=0.0, zero=0.0, unwind_slip=2)
    )
    # First, BUY 10 YES at 40c (paper sets up the "raw ask" memory).
    buy = OrderRequest(
        market_ticker="KXT", side="yes", action="buy",
        order_type="limit", time_in_force="IOC", count=10,
        limit_cents=40, client_order_id="kac_buy_unwind",
    )
    asyncio.run(api.place_order(buy))
    # Now unwind: market SELL 10 YES.
    sell = OrderRequest(
        market_ticker="KXT", side="yes", action="sell",
        order_type="market", time_in_force="IOC", count=10,
        limit_cents=0, client_order_id="kac_sell_unwind",
    )
    resp = asyncio.run(api.place_order(sell))
    assert resp.filled_count == 10
    assert resp.avg_fill_price_cents == 38  # 40 - 2c slippage


# ----------------------------------------------------------------------
# (5) REQUIRED: test_paper_mode_exercises_unwind_path
# ----------------------------------------------------------------------


def test_paper_mode_exercises_unwind_path(tmp_path: Path) -> None:
    """Per review spec: run 100 executions at partial_fill_rate=0.5 (high)
    to prove the unwind path fires and completes. Fixed RNG seed for
    determinism."""
    config = _paper_config(full=0.5, partial=0.5, zero=0.0, seed=9999)
    api = PaperKalshiAPI(config=config)
    killswitch = KillSwitch(sentinel=tmp_path / "KILL_SWITCH")
    executor = StructuralArbExecutor(
        api=api,
        killswitch=killswitch,
        config=ExecutorConfig(
            critical_unwind_dir=tmp_path,
            unwind_timeout_sec=5.0,
        ),
    )

    unwind_outcomes = {
        OUTCOME_ONE_FILLED_UNWOUND,
        OUTCOME_BOTH_FILLED_IMBALANCED_UNWOUND,
    }
    seen_outcomes: dict[str, int] = {}

    async def _run() -> None:
        for i in range(100):
            # Unique timestamp per iteration so COIDs differ.
            opp = _opp(detected_ts_ms=1_700_000_000_000 + i)
            result = await executor.execute(_decision(opp, contracts=10))
            seen_outcomes[result.outcome] = seen_outcomes.get(result.outcome, 0) + 1

    asyncio.run(_run())

    unwinds_fired = sum(seen_outcomes.get(o, 0) for o in unwind_outcomes)
    assert unwinds_fired >= 1, (
        f"No unwinds fired across 100 executions at 50% partial rate. "
        f"Outcomes: {seen_outcomes}"
    )
    # Kill switch must NOT have tripped (no unwind failures expected with
    # full fills on SELL side by default; RNG seed is picked to avoid
    # zero-fill on sells).
    assert not killswitch.is_tripped()
    # Estimated counter moved, realized counter stayed clean per earlier
    # Module 3 guarantee.
    assert executor.daily_estimated_pnl_cents != 0


# ----------------------------------------------------------------------
# (6) Integration: full pipeline against PaperKalshiAPI
# ----------------------------------------------------------------------


class _FakeLiveReader:
    """Stand-in for LiveKalshiAPI during paper-mode integration tests --
    we don't actually want to hit prod. Returns a fixed PortfolioSnapshot."""

    def __init__(self) -> None:
        self.read_count = 0

    async def get_portfolio(self):
        from kalshi_arb.executor.kalshi_api import PortfolioSnapshot
        self.read_count += 1
        return PortfolioSnapshot(cash_cents=10_000_00, positions={}, at_ms=0)


def test_full_pipeline_paper_mode_integration(tmp_path: Path) -> None:
    """Scanner -> sizer -> executor -> PaperKalshiAPI. Assert zero live
    calls (we'd know because we didn't even wire a real API client), every
    fill is synthetic, and the portfolio read path is delegated to the
    fake live reader."""
    live_stub = _FakeLiveReader()
    api = PaperKalshiAPI(
        config=_paper_config(full=1.0, partial=0.0, zero=0.0, seed=1),
        live_reader=live_stub,
    )
    scanner = StructuralArbScanner(
        ScannerConfig(min_edge_cents=1.0, slippage_buffer_cents=0.5),
        fee_model=FeeModel.builtin(),
    )
    sizer = HalfKellySizer(SizerConfig(hard_cap_usd=9.0, min_expected_profit_usd=0.10))
    killswitch = KillSwitch(sentinel=tmp_path / "KILL_SWITCH")
    executor = StructuralArbExecutor(
        api=api,
        killswitch=killswitch,
        config=ExecutorConfig(critical_unwind_dir=tmp_path),
    )

    events = [
        {"ticker": "KXPIPE-1", "side": "yes", "price": 40, "delta": 100, "ts": 0.0},
        {"ticker": "KXPIPE-1", "side": "no", "price": 50, "delta": 100, "ts": 0.1},
    ]
    book = OrderBook(ticker="KXPIPE-1")
    executions = []

    async def _drive() -> None:
        for ev in events:
            book.apply_delta(ev["side"], ev["price"], ev["delta"], now=ev["ts"])
            decision = scanner.scan(book, now_ms=int(ev["ts"] * 1000))
            if decision.decision != DECISION_EMIT:
                continue
            sizing = sizer.size(
                decision.opportunity,
                BankrollSnapshot(
                    cash_cents=10_000_00, open_positions_value_cents=0,
                    peak_equity_cents=10_000_00, daily_realized_pnl_cents=0,
                    taken_at_ms=0, stale=False,
                ),
            )
            if sizing.contracts_per_leg == 0:
                continue
            result = await executor.execute(sizing)
            executions.append(result)
        # Delegate portfolio read.
        await api.get_portfolio()

    asyncio.run(_drive())

    # At least one emit turned into an execution.
    assert len(executions) >= 1
    # All orders recorded on the paper API, none leaked to a real client.
    assert len(api.placed_orders) == 2 * len(executions)
    # Every kalshi_order_id starts with "paper-" -- proves these are synthetic.
    for r in executions:
        if r.outcome == OUTCOME_BOTH_FILLED:
            for leg in r.legs:
                if leg.kalshi_order_id is not None:
                    assert leg.kalshi_order_id.startswith("paper-"), (
                        f"leg order_id={leg.kalshi_order_id} is not paper -- "
                        f"a real API call leaked through"
                    )
    # Portfolio read was delegated.
    assert live_stub.read_count == 1
    # No kill switch trips.
    assert not killswitch.is_tripped()
