"""Event store tests."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from kalshi_arb.store.db import EventStore, WriteJob


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


@pytest.mark.asyncio
async def test_event_store_writes_and_reads(tmp_db: Path) -> None:
    store = EventStore(tmp_db)
    await store.start()
    try:
        store.record_orderbook_event("KXTEST-1", 1, "yes", 45, 10, "delta")
        store.record_orderbook_event("KXTEST-1", 2, "yes", 45, -3, "delta")
        store.record_orderbook_event("KXTEST-1", 3, "no", 52, 5, "delta")
        # Allow writer to drain
        await asyncio.sleep(0.3)
        rows = store.read_many(
            "SELECT ticker, seq, side, price, delta FROM orderbook_events ORDER BY seq"
        )
        assert rows == [
            ("KXTEST-1", 1, "yes", 45, 10),
            ("KXTEST-1", 2, "yes", 45, -3),
            ("KXTEST-1", 3, "no", 52, 5),
        ]
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_event_store_upsert_market(tmp_db: Path) -> None:
    store = EventStore(tmp_db)
    await store.start()
    try:
        store.upsert_market(
            {
                "ticker": "KXBTC15M-26APR18-T74999.99",
                "series_ticker": "KXBTC15M",
                "event_ticker": "KXBTC15M-26APR18",
                "title": "BTC 15m",
                "subtitle": "above $75k",
                "category": "crypto",
                "status": "open",
                "close_ts_ms": 1234567890000,
            }
        )
        await asyncio.sleep(0.3)
        row = store.read_one(
            "SELECT ticker, series_ticker, category, status FROM markets WHERE ticker=?",
            ("KXBTC15M-26APR18-T74999.99",),
        )
        assert row == ("KXBTC15M-26APR18-T74999.99", "KXBTC15M", "crypto", "open")

        # Upsert to 'closed' and confirm update
        store.upsert_market(
            {
                "ticker": "KXBTC15M-26APR18-T74999.99",
                "series_ticker": "KXBTC15M",
                "status": "closed",
            }
        )
        await asyncio.sleep(0.3)
        row2 = store.read_one(
            "SELECT status FROM markets WHERE ticker=?",
            ("KXBTC15M-26APR18-T74999.99",),
        )
        assert row2 == ("closed",)
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_event_store_ws_metrics(tmp_db: Path) -> None:
    store = EventStore(tmp_db)
    await store.start()
    try:
        store.record_ws_metric(
            bucket_ts_ms=1_000_000,
            ticker="KXTEST",
            msg_count=42,
            gap_count=1,
            last_seq=99,
            last_msg_ms=1_000_042,
        )
        await asyncio.sleep(0.3)
        row = store.read_one(
            "SELECT bucket_ts_ms, ticker, msg_count, gap_count FROM ws_metrics"
        )
        assert row == (1_000_000, "KXTEST", 42, 1)
    finally:
        await store.stop()
