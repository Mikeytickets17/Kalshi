"""End-to-end paper-mode integration test.

Drives: synthetic event stream -> scanner -> sizer -> executor against
FakeKalshiAPI. Zero real network. Asserts the complete decision trail
lands in the fake event store (one decision row per scanner evaluation,
one order per fired leg)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from kalshi_arb.executor import (
    ExecutorConfig,
    KillSwitch,
    OUTCOME_BOTH_FILLED,
    OUTCOME_BOTH_REJECTED,
    PNL_REALIZED,
    StructuralArbExecutor,
)
from kalshi_arb.scanner import (
    DECISION_EMIT,
    FeeModel,
    OrderBook,
    ScanDecision,
    ScannerConfig,
    StructuralArbScanner,
)
from kalshi_arb.sizer import BankrollSnapshot, HalfKellySizer, SizerConfig

from tests.fakes import FakeKalshiAPI, policy_both_fill_fully


# ---------------------------------------------------------------------
# Minimal in-memory event-store stub. The real SQLite store is tested
# elsewhere; here we only assert the pipeline WROTE decisions + orders
# through its callback interfaces.
# ---------------------------------------------------------------------


@dataclass
class FakeEventStore:
    decisions: list[ScanDecision] = field(default_factory=list)
    execution_results: list = field(default_factory=list)

    def record_scan(self, decision: ScanDecision) -> None:
        self.decisions.append(decision)

    def record_execution(self, result) -> None:
        self.execution_results.append(result)


def _bank(cash_cents: int = 10_000_000) -> BankrollSnapshot:
    return BankrollSnapshot(
        cash_cents=cash_cents,
        open_positions_value_cents=0,
        peak_equity_cents=cash_cents,
        daily_realized_pnl_cents=0,
        taken_at_ms=0,
        stale=False,
    )


def test_scanner_sizer_executor_end_to_end_paper(tmp_path: Path) -> None:
    """Full pipeline against synthetic events. Asserts:
      - every synth event produces exactly one ScanDecision
      - every DECISION_EMIT that the sizer accepts produces exactly one
        ExecutionResult with outcome in (both_filled, both_rejected)
      - no real network calls
      - no unexpected kill-switch trips
    """
    # --- wire up ---
    store = FakeEventStore()
    scanner = StructuralArbScanner(
        ScannerConfig(min_edge_cents=1.0, slippage_buffer_cents=0.5),
        fee_model=FeeModel.builtin(),
        on_decision=store.record_scan,
    )
    sizer = HalfKellySizer(
        SizerConfig(hard_cap_usd=50.0, min_expected_profit_usd=0.10)
    )
    killswitch = KillSwitch(sentinel=tmp_path / "KILL_SWITCH")
    api = FakeKalshiAPI(fill_policy=policy_both_fill_fully)
    executor = StructuralArbExecutor(
        api=api,
        killswitch=killswitch,
        config=ExecutorConfig(critical_unwind_dir=tmp_path),
    )

    # --- synthetic events: one market, a few books ---
    events = [
        # Build up a populated book over a few deltas.
        {"ticker": "KXPIPE-1", "side": "yes", "price": 40, "delta": 100, "ts": 0.0},
        {"ticker": "KXPIPE-1", "side": "no",  "price": 50, "delta": 100, "ts": 0.1},
        # Still 90c sum -- scanner will see the same arb on each update.
        {"ticker": "KXPIPE-1", "side": "yes", "price": 40, "delta": 50,  "ts": 0.2},
        {"ticker": "KXPIPE-1", "side": "no",  "price": 50, "delta": 50,  "ts": 0.3},
    ]

    book = OrderBook(ticker="KXPIPE-1")
    emits = 0
    executions = 0
    total_orders_placed = 0

    async def _drive() -> None:
        nonlocal emits, executions, total_orders_placed
        for ev in events:
            book.apply_delta(ev["side"], ev["price"], ev["delta"], now=ev["ts"])
            decision = scanner.scan(book, now_ms=int(ev["ts"] * 1000))
            if decision.decision != DECISION_EMIT:
                continue
            emits += 1
            # Note: scanner already recorded the decision via on_decision.
            sizing = sizer.size(decision.opportunity, _bank())
            if sizing.contracts_per_leg == 0:
                continue
            result = await executor.execute(sizing)
            store.record_execution(result)
            executions += 1
            total_orders_placed = len(api.placed_orders)

    asyncio.run(_drive())

    # --- assertions ---
    # Each event produced exactly one ScanDecision.
    assert len(store.decisions) == len(events)
    # At least one emit (books got to arb-able state).
    assert emits > 0
    # Every emit that cleared the sizer resulted in exactly one execution.
    assert executions == len(store.execution_results)
    # Every execution placed exactly 2 orders (yes + no). Duplicate emits
    # with the SAME detected_ts_ms would dedupe, but here each event has a
    # different ts_ms so each execute() hits 2 new COIDs.
    assert total_orders_placed == 2 * executions
    # No kill switch trips during the run.
    assert not killswitch.is_tripped()
    # Clean-arb P&L confidence.
    for r in store.execution_results:
        assert r.outcome in (OUTCOME_BOTH_FILLED, OUTCOME_BOTH_REJECTED)
        if r.outcome == OUTCOME_BOTH_FILLED:
            assert r.pnl_confidence == PNL_REALIZED
