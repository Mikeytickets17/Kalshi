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
    """Full pipeline against synthetic events with a HAND-CALCULATED
    expected P&L assertion (review Flag 3).

    Deterministic setup:
      - Book: YES=40c, NO=50c, 100 qty each side -> sum=90c.
      - Scanner fees (both at 45c-ish range): ceil(0.07*0.40*0.60)=2c,
        ceil(0.07*0.50*0.50)=2c. Total fees per pair = 4c.
      - Scanner slippage buffer = 0.5c.
      - net_edge = 100 - 90 - 4 - 0.5 = 5.5c per contract.
      - Sizer hard_cap=$9 so hard_cap_size = floor(900/90) = 10 contracts.
      - Liquidity cap = 100, Kelly is huge ($10k bankroll), so hard_cap
        wins. contracts_per_leg = 10.
      - FakeKalshiAPI policy_both_fill_fully fills all 10 at limit:
        YES fills 10 @ 40c, NO fills 10 @ 50c.
        fees from fake = 2c per contract per leg = 40c total per execution.
      - net_fill_cents (our conservative buy-only measure) = 10*40 + 10*50 = 900c.
      - At settlement: one side pays $1 x 10 contracts = 1000c.
      - Expected realized P&L per execution = 1000 - 900 - 40 = 60c.
    """
    store = FakeEventStore()
    scanner = StructuralArbScanner(
        ScannerConfig(min_edge_cents=1.0, slippage_buffer_cents=0.5),
        fee_model=FeeModel.builtin(),
        on_decision=store.record_scan,
    )
    sizer = HalfKellySizer(
        SizerConfig(
            hard_cap_usd=9.0,         # forces hard_cap=10 contracts
            min_expected_profit_usd=0.10,
        )
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

    async def _drive() -> None:
        nonlocal emits, executions
        for ev in events:
            book.apply_delta(ev["side"], ev["price"], ev["delta"], now=ev["ts"])
            decision = scanner.scan(book, now_ms=int(ev["ts"] * 1000))
            if decision.decision != DECISION_EMIT:
                continue
            emits += 1
            sizing = sizer.size(decision.opportunity, _bank())
            if sizing.contracts_per_leg == 0:
                continue
            result = await executor.execute(sizing)
            store.record_execution(result)
            executions += 1

    asyncio.run(_drive())

    # --- structural assertions ---
    assert len(store.decisions) == len(events)
    assert emits > 0
    assert executions == len(store.execution_results)
    assert len(api.placed_orders) == 2 * executions
    assert not killswitch.is_tripped()

    # --- MATH assertions (Flag 3) ---
    EXPECTED_CONTRACTS_PER_LEG = 10     # from hard_cap=$9/$0.90
    EXPECTED_NET_FILL_CENTS = 10 * 40 + 10 * 50   # 900 (buy cost only)
    EXPECTED_FEES_CENTS = 2 * 10 + 2 * 10         # 40 (FakeKalshi: 2c per contract per leg)
    EXPECTED_PNL_PER_EXECUTION = 10 * 100 - EXPECTED_NET_FILL_CENTS - EXPECTED_FEES_CENTS  # 60

    filled_executions = [
        r for r in store.execution_results if r.outcome == OUTCOME_BOTH_FILLED
    ]
    assert len(filled_executions) > 0, "no filled executions to check math on"

    for r in filled_executions:
        # Sizer produced the exact contract count we computed by hand.
        assert r.decision.contracts_per_leg == EXPECTED_CONTRACTS_PER_LEG, (
            f"sizer returned {r.decision.contracts_per_leg} contracts, "
            f"expected {EXPECTED_CONTRACTS_PER_LEG} from hard_cap=$9/$0.90"
        )
        # Net-fill accounting matches hand calculation.
        assert r.net_fill_cents == EXPECTED_NET_FILL_CENTS, (
            f"net_fill_cents={r.net_fill_cents} != expected "
            f"{EXPECTED_NET_FILL_CENTS} (10*40c YES + 10*50c NO). "
            f"The executor's cost accounting is drifting."
        )
        # Fees match the fake's policy (2c per contract per leg).
        assert r.total_fees_cents == EXPECTED_FEES_CENTS, (
            f"total_fees_cents={r.total_fees_cents} != {EXPECTED_FEES_CENTS}"
        )
        # Clean arb -> realized confidence.
        assert r.pnl_confidence == PNL_REALIZED

    # The executor's running realized-P&L counter equals N × expected P&L.
    expected_running = len(filled_executions) * EXPECTED_PNL_PER_EXECUTION
    assert executor.daily_realized_pnl_cents == expected_running, (
        f"daily_realized_pnl_cents={executor.daily_realized_pnl_cents}c != "
        f"expected {expected_running}c "
        f"({len(filled_executions)} executions × {EXPECTED_PNL_PER_EXECUTION}c each). "
        f"Structural arb accounting is broken."
    )
    # Estimated counter must stay at 0 (no unwinds happened).
    assert executor.daily_estimated_pnl_cents == 0
