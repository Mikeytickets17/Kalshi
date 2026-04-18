"""Populate a fresh SQLite event store with a deterministic fixture
dataset that exercises every tab and drawer in the dashboard.

The same populator is used by:
  * tests/test_dashboard_tabs.py -- integration tests that assert
    specific rendered content per tab
  * verify_dashboard.py / verify_dashboard.bat -- the operator's
    one-click pre-push verifier

Determinism matters: populate() runs against an empty SQLite file and
writes the same row counts / ids every time, so tests can assert exact
rendered content.

Populated tables (in write order):
  * markets             -- 5 rows across 3 categories
  * opportunities_detected -- 12 rows (5 emit + 7 skip)
  * orders_placed       -- 10 (2 legs * 5 emitted)
  * orders_filled       -- 10
  * pnl_realized        -- 5 (3 wins, 1 loss, 1 breakeven)
  * ws_metrics          -- 6 (3 tickers * 2 buckets)
  * probe_runs          -- 4 (demo env)
  * change_log          -- 1 per domain write + 2 kill_switch + 2 degraded

The coroutine submits everything to the store's writer queue, then
awaits a drain so every row is durably on disk before returning. The
populator opens + closes its own EventStore; it does not share state
with the dashboard process.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from kalshi_arb.store import EventStore, SqliteBackend


FIXTURE_TICKERS = [
    ("KXBTC-NOV", "crypto", "BTC close above 40k"),
    ("KXETH-NOV", "crypto", "ETH close above 3k"),
    ("KXRAIN-NYC", "weather", "Rain in NYC tomorrow"),
    ("KXCPI-OCT", "econ", "CPI print > 3.1%"),
    ("KXFED-DEC", "econ", "Fed cuts 25bp in December"),
]

# (ticker_index, hours_ago, yes, no, edge_cents, final_size)
EMITTED_SPECS = [
    (0, 6, 42, 55, 3.2, 20),
    (1, 5, 44, 53, 2.5, 15),
    (2, 4, 47, 50, 1.8, 10),
    (3, 3, 45, 52, 2.0, 12),
    (4, 1, 46, 51, 2.7, 18),
]
# (ticker_index, hours_ago, yes, no, edge_cents, decision)
SKIP_SPECS = [
    (0, 20, 49, 50, 0.3, "skip_below_edge"),
    (1, 18, 48, 50, 0.5, "skip_below_edge"),
    (2, 12, 48, 51, 0.7, "skip_below_profit"),
    (3, 10, 51, 50, -0.5, "skip_below_edge"),
    (4, 8, 0, 0, 0.0, "skip_empty_side"),
    (0, 2, 49, 52, 0.1, "skip_below_edge"),
    (1, 30, 47, 53, 1.2, "skip_halted"),
]
# (opp_index_within_emitted, yes_pnl, no_pnl, fees, net, note)
PNL_SPECS = [
    (0, 120, 0, 2, 118, "full settle, arb took"),
    (1, 80, 0, 2, 78, "both legs paid"),
    (2, -40, 30, 2, -12, "one leg lost, partial unwind"),
    (3, 0, 0, 2, -2, "fees ate the edge"),
    (4, 95, -10, 2, 83, "net positive after slippage"),
]
PROBE_SPECS = [
    ("demo", "auth", "pass", 42, None),
    ("demo", "rest", "pass", 55, None),
    ("demo", "ws", "pass", 70, None),
    ("demo", "order_lifecycle", "fail", None, "429 rate limited"),
]
DEGRADED_SPECS = [
    ("ws_reconnect_storm", "3 reconnects in 60s"),
    ("slow_probe", "order_lifecycle > 30s"),
]


@dataclass
class PopulatedCounts:
    markets: int
    opportunities: int
    orders_placed: int
    orders_filled: int
    pnl_rows: int
    probe_runs: int
    degraded_events: int
    kill_switch_events: int


def _ms(days_ago: int = 0, hours_ago: int = 0, minutes_ago: int = 0) -> int:
    dt = datetime.now(tz=UTC) - timedelta(
        days=days_ago, hours=hours_ago, minutes=minutes_ago
    )
    return int(dt.timestamp() * 1000)


async def populate_async(db_path: Path) -> PopulatedCounts:
    """Idempotent-ish: writes are additive. Use a fresh db_path per
    invocation (tmp_path in tests). Blocks until the writer has drained."""
    store = EventStore(SqliteBackend(db_path))
    await store.start()
    try:
        return await _write_all(store)
    finally:
        # store.stop() joins the writer queue + closes the backend.
        await store.stop()


def populate(db_path: Path) -> PopulatedCounts:
    """Sync wrapper. NOT safe inside a running event loop -- use
    populate_async for that case."""
    return asyncio.run(populate_async(db_path))


async def _write_all(store: EventStore) -> PopulatedCounts:
    # --- 1. Markets ---------------------------------------------------
    now = _ms()
    for ticker, cat, title in FIXTURE_TICKERS:
        store.upsert_market({
            "ticker": ticker,
            "series_ticker": ticker.split("-")[0],
            "event_ticker": ticker,
            "title": title,
            "subtitle": None,
            "category": cat,
            "status": "open",
            "open_ts_ms": now - 86_400_000,
            "close_ts_ms": now + 86_400_000,
        })

    # --- 2. Opportunities (emit + skip) ------------------------------
    for ticker_idx, hours_ago, yes, no, edge, size in EMITTED_SPECS:
        store.record_opportunity(
            ticker=FIXTURE_TICKERS[ticker_idx][0],
            ts_ms=_ms(hours_ago=hours_ago),
            yes_ask_cents=yes, yes_ask_qty=100,
            no_ask_cents=no, no_ask_qty=100,
            sum_cents=yes + no,
            est_fees_cents=3, slippage_buffer=1,
            net_edge_cents=edge,
            max_size_liquidity=100,
            kelly_size=size * 2, hard_cap_size=size, final_size=size,
            decision="emit",
        )
    for ticker_idx, hours_ago, yes, no, edge, reason in SKIP_SPECS:
        store.record_opportunity(
            ticker=FIXTURE_TICKERS[ticker_idx][0],
            ts_ms=_ms(hours_ago=hours_ago),
            yes_ask_cents=yes, yes_ask_qty=(100 if yes else 0),
            no_ask_cents=no, no_ask_qty=(100 if no else 0),
            sum_cents=yes + no,
            est_fees_cents=3, slippage_buffer=1,
            net_edge_cents=edge,
            max_size_liquidity=100,
            kelly_size=0, hard_cap_size=0, final_size=0,
            decision=reason, rejection_reason=reason,
        )

    # Drain so subsequent reads (to resolve opportunity ids for the
    # orders_placed FK) see the just-written rows.
    await _drain(store)

    # Look up ids by ticker+ts_ms signature (both unique within fixture).
    emitted_ids: list[int] = []
    for ticker_idx, hours_ago, *_ in EMITTED_SPECS:
        ticker = FIXTURE_TICKERS[ticker_idx][0]
        # The exact ts_ms we wrote:
        ts_expected = _ms(hours_ago=hours_ago)
        row = store.read_one(
            "SELECT id FROM opportunities_detected"
            " WHERE ticker = ? AND decision = 'emit'"
            " ORDER BY ABS(ts_ms - ?) ASC LIMIT 1",
            (ticker, ts_expected),
        )
        if not row:
            raise RuntimeError(
                f"fixture: emitted opportunity for {ticker} not found"
            )
        emitted_ids.append(int(row[0]))

    # --- 3. Orders + fills -------------------------------------------
    placed = 0
    filled = 0
    for opp_id, (ticker_idx, _hours, yes, no, _edge, size) in zip(
        emitted_ids, EMITTED_SPECS, strict=True
    ):
        ticker = FIXTURE_TICKERS[ticker_idx][0]
        yes_coid = f"FIX-{opp_id:04d}-Y"
        no_coid = f"FIX-{opp_id:04d}-N"
        store.record_order_placed(
            client_order_id=yes_coid, kalshi_order_id=f"KX-YES-{opp_id}",
            opportunity_id=opp_id, ticker=ticker,
            side="yes", action="buy", type_="limit",
            limit_price=yes, count=size, placed_ok=True, error=None,
        )
        store.record_order_placed(
            client_order_id=no_coid, kalshi_order_id=f"KX-NO-{opp_id}",
            opportunity_id=opp_id, ticker=ticker,
            side="no", action="buy", type_="limit",
            limit_price=no, count=size, placed_ok=True, error=None,
        )
        placed += 2
        store.record_order_filled(
            client_order_id=yes_coid, filled_price=yes,
            filled_count=size, fees_cents=1,
        )
        store.record_order_filled(
            client_order_id=no_coid, filled_price=no,
            filled_count=size, fees_cents=1,
        )
        filled += 2

    # --- 4. Realized P&L ---------------------------------------------
    for idx, yes_pnl, no_pnl, fees, net, note in PNL_SPECS:
        store.record_pnl_realized(
            opportunity_id=emitted_ids[idx],
            yes_pnl_cents=yes_pnl,
            no_pnl_cents=no_pnl,
            fees_cents=fees,
            net_cents=net,
            note=note,
        )

    # --- 5. WS metrics -----------------------------------------------
    for idx, (ticker, _cat, _title) in enumerate(FIXTURE_TICKERS[:3]):
        for offset_min in (2, 0):
            store.record_ws_metric(
                bucket_ts_ms=now - offset_min * 60_000,
                ticker=ticker,
                msg_count=120 - offset_min * 5,
                gap_count=1 if (offset_min == 2 and idx == 0) else 0,
                last_seq=5000 + idx * 100 + (0 if offset_min else 10),
                last_msg_ms=now - offset_min * 60_000,
            )

    # --- 6. Probe runs -----------------------------------------------
    for env, name, status, latency, err in PROBE_SPECS:
        store.record_probe_run(
            env_tag=env, probe_name=name, status=status,
            latency_ms=latency, error=err,
        )

    # --- 7. Degraded events ------------------------------------------
    for kind, detail in DEGRADED_SPECS:
        store.record_degraded_event(kind, detail)

    # --- 8. Kill-switch trip + reset ---------------------------------
    store.record_kill_switch_change(tripped=True, reason="fixture-trip")
    store.record_kill_switch_change(tripped=False, reason="fixture-reset")

    await _drain(store)

    return PopulatedCounts(
        markets=len(FIXTURE_TICKERS),
        opportunities=len(EMITTED_SPECS) + len(SKIP_SPECS),
        orders_placed=placed,
        orders_filled=filled,
        pnl_rows=len(PNL_SPECS),
        probe_runs=len(PROBE_SPECS),
        degraded_events=len(DEGRADED_SPECS),
        kill_switch_events=2,
    )


async def _drain(store: EventStore) -> None:
    """Wait for the writer coroutine to drain its queue. The store's
    internal _queue.join() waits for task_done on every submitted job.
    We check the private member because the public API only drains
    on stop()."""
    q = store._queue  # type: ignore[attr-defined]
    if q is not None:
        await q.join()
    # One more tick so the final execute() returns to the OS / WAL.
    await asyncio.sleep(0.05)
