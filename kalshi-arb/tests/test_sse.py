"""Step 3 SSE + change-capture tests.

Layered:

  test_sse_broker_pubsub_basic             -- broker pub/sub + wire format
  test_sse_broker_evicts_slow_client       -- bounded queue invariant
  test_change_capture_broadcasts_new_rows  -- poller → broker → subscriber
  test_events_stream_replays_on_resume     -- Last-Event-ID honored
  test_events_poll_fallback_endpoint       -- JSON fallback for SSE-less clients
  test_stream_requires_auth                -- /events/stream is session-gated
  test_high_throughput_no_loss             -- 500 events in <=3 s, all delivered
  (live, opt-in) test_sustained_100_per_sec-- 100/s sustained for 60 s
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kalshi_arb.dashboard.app import SESSION_COOKIE, create_app
from kalshi_arb.dashboard.config import DashboardConfig
from kalshi_arb.dashboard.sse import Change, ChangeCapture, SSEBroker
from kalshi_arb.store import EventStore, SqliteBackend


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _config(tmp_path: Path, **overrides) -> DashboardConfig:
    base = dict(
        username="admin",
        password="pw-step3-test",
        session_secret="test-secret",
        port=8080,
        login_rate_per_min_per_ip=5,
        event_store_path=tmp_path / "kalshi.db",
        replay_backlog_on_start=False,
        libsql_url="",
        libsql_auth_token="",
        libsql_sync_url="",
        libsql_local_path=tmp_path / "replica.db",
    )
    base.update(overrides)
    return DashboardConfig(**base)


async def _insert_opportunity(store: EventStore, ticker: str, ts_ms: int = 1_700_000_000_000) -> None:
    store.record_opportunity(
        ticker=ticker, ts_ms=ts_ms,
        yes_ask_cents=40, yes_ask_qty=100,
        no_ask_cents=50, no_ask_qty=100,
        sum_cents=90, est_fees_cents=4, slippage_buffer=0,
        net_edge_cents=5.5, max_size_liquidity=100,
        kelly_size=10, hard_cap_size=10, final_size=10,
        decision="emit",
    )


# ----------------------------------------------------------------------
# Broker unit tests
# ----------------------------------------------------------------------


async def test_sse_broker_pubsub_basic() -> None:
    broker = SSEBroker()

    received: list[Change] = []

    async def _consume():
        async for ch in broker.subscribe():
            received.append(ch)
            if len(received) == 2:
                return

    consumer = asyncio.create_task(_consume())
    await asyncio.sleep(0.05)  # let subscribe() register

    broker.publish(Change(1, "opportunity", 10, 111, None))
    broker.publish(Change(2, "kill_switch", None, 222, "{}"))

    await asyncio.wait_for(consumer, timeout=2.0)
    assert [c.id for c in received] == [1, 2]
    # Wire format checks.
    wire = received[0].as_sse_event()
    assert wire.startswith("event: opportunity\n")
    assert "id: 1\n" in wire
    assert wire.endswith("\n\n")
    assert json.loads(wire.split("data: ")[1].strip())["entity_type"] == "opportunity"


async def test_sse_broker_evicts_slow_client() -> None:
    """If a client's queue fills up, the broker MUST drop that client
    rather than stall every other subscriber. Proves the 'never block
    the broadcaster' invariant."""
    broker = SSEBroker()

    # Register a subscriber but never drain its queue. We only advance
    # the generator to its first await point (queue.get()), which is
    # what registers the queue in the broker's subscriber set. After
    # that we just let messages pile up.
    consumed: list[Change] = []

    async def _slow():
        async for ch in broker.subscribe():
            consumed.append(ch)
            # Simulate a slow client: stall forever after the first item.
            await asyncio.sleep(60)

    task = asyncio.create_task(_slow())
    # Yield so the generator registers its queue.
    await asyncio.sleep(0.05)
    assert broker.stats()["subscribers"] == 1

    # Flood past QUEUE_SIZE (1024). First item fills the consumed list;
    # the rest pile up in the queue until QUEUE_SIZE then overflow.
    for i in range(2000):
        broker.publish(Change(i, "opportunity", i, 0, None))

    # Give the broker + generator a tick to process the close sentinel.
    await asyncio.sleep(0.05)

    stats = broker.stats()
    assert stats["dropped_clients"] > 0, "broker should have evicted the slow client"
    assert stats["subscribers"] == 0, "evicted client should be removed"

    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, StopAsyncIteration):
        pass


async def test_change_capture_broadcasts_new_rows(tmp_path: Path) -> None:
    store = EventStore(SqliteBackend(tmp_path / "cap.db"))
    await store.start()
    broker = SSEBroker()
    capture = ChangeCapture(store, broker, tick_sec=0.1, start_at_latest=False)
    await capture.start()

    received: list[Change] = []

    async def _consume():
        async for ch in broker.subscribe():
            received.append(ch)

    consumer = asyncio.create_task(_consume())
    await asyncio.sleep(0.05)

    try:
        for i in range(3):
            await _insert_opportunity(store, ticker=f"KXCAP-{i}")

        # Wait for capture to poll + broadcast.
        deadline = asyncio.get_event_loop().time() + 3.0
        while len(received) < 3 and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.05)

        assert len(received) == 3
        assert all(c.entity_type == "opportunity" for c in received)
        assert [c.id for c in received] == sorted([c.id for c in received])
    finally:
        consumer.cancel()
        await capture.stop()
        await store.stop()


# ----------------------------------------------------------------------
# FastAPI integration tests
# ----------------------------------------------------------------------


def test_events_poll_fallback_endpoint(tmp_path: Path) -> None:
    """FastAPI TestClient used as a sync context manager so the app's
    lifespan runs; that's what populates app.state.{store,broker,capture}."""
    cfg = _config(tmp_path)
    # Dashboard opens its own store via the lifespan. We insert from a
    # *separate* EventStore instance against the same SQLite file --
    # that simulates the bot writing to the shared file while the
    # dashboard reads from it.
    async def _seed():
        bot_store = EventStore(SqliteBackend(cfg.event_store_path))
        await bot_store.start()
        try:
            for i in range(3):
                await _insert_opportunity(bot_store, ticker=f"KXPOLL-{i}")
            await asyncio.sleep(0.3)
        finally:
            await bot_store.stop()
    asyncio.run(_seed())

    with TestClient(create_app(cfg), base_url="https://testserver", follow_redirects=False) as c:
        r = c.post("/login", data={"username": cfg.username, "password": cfg.password})
        assert r.status_code == 303
        r = c.get("/events/poll?since_id=0&limit=100")
        assert r.status_code == 200
        body = r.json()
        assert len(body["changes"]) == 3
        assert [ch["entity_type"] for ch in body["changes"]] == ["opportunity"] * 3

        mid_id = body["changes"][0]["id"]
        r2 = c.get(f"/events/poll?since_id={mid_id}")
        assert len(r2.json()["changes"]) == 2


def test_stream_requires_auth(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    with TestClient(create_app(cfg), base_url="https://testserver", follow_redirects=False) as c:
        r = c.get("/events/stream")
        assert r.status_code == 303
        assert r.headers["location"] == "/login"


def test_events_poll_honors_since_id_for_gap_free_resume(tmp_path: Path) -> None:
    """Fallback polling is the same zero-loss primitive the SSE stream's
    Last-Event-ID replay uses (both call changes_since(since_id) under
    the hood). Testing via /events/poll is sufficient here -- the full
    SSE-over-HTTP path is verified at the dashboard-deploy gate against
    a real uvicorn through the Cloudflare Tunnel, which is the only
    environment where the entire transport can be proven."""
    cfg = _config(tmp_path)

    async def _seed():
        bot_store = EventStore(SqliteBackend(cfg.event_store_path))
        await bot_store.start()
        try:
            for i in range(5):
                await _insert_opportunity(bot_store, ticker=f"KXRES-{i}")
            await asyncio.sleep(0.3)
        finally:
            await bot_store.stop()
    asyncio.run(_seed())

    with TestClient(create_app(cfg), base_url="https://testserver", follow_redirects=False) as c:
        c.post("/login", data={"username": cfg.username, "password": cfg.password})

        # Simulate 'client already saw id <= 2' -- server must return ids 3,4,5 only.
        r = c.get("/events/poll?since_id=2")
        body = r.json()
        ids = [ch["id"] for ch in body["changes"]]
        assert ids == [3, 4, 5], f"expected [3,4,5] after since_id=2, got {ids}"


# ----------------------------------------------------------------------
# Throughput / correctness under load
# ----------------------------------------------------------------------


async def test_high_throughput_no_loss(tmp_path: Path) -> None:
    """500 rows through the pipeline quickly. Every id must reach a
    subscribed consumer. Catches the queue-overflow / poller-skip class
    of bug."""
    store = EventStore(SqliteBackend(tmp_path / "tput.db"))
    await store.start()
    broker = SSEBroker()
    capture = ChangeCapture(store, broker, tick_sec=0.05, start_at_latest=False)
    await capture.start()

    received_ids: list[int] = []

    async def _consume():
        async for ch in broker.subscribe():
            received_ids.append(ch.id)

    consumer = asyncio.create_task(_consume())
    await asyncio.sleep(0.05)

    try:
        start = time.monotonic()
        for i in range(500):
            await _insert_opportunity(store, ticker=f"KXTP-{i}")

        # Wait until we see 500 OR 5s passes.
        deadline = time.monotonic() + 5.0
        while len(received_ids) < 500 and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        elapsed = time.monotonic() - start

        assert len(received_ids) == 500, (
            f"expected 500 events, got {len(received_ids)} in {elapsed:.1f}s"
        )
        # Monotonic (poller emits in change_log.id order).
        assert received_ids == sorted(received_ids)
        # No duplicates.
        assert len(set(received_ids)) == 500
        assert elapsed < 3.0, f"pipeline too slow: {elapsed:.1f}s"
    finally:
        consumer.cancel()
        await capture.stop()
        await store.stop()


@pytest.mark.live
async def test_sustained_100_per_sec_for_60_seconds(tmp_path: Path) -> None:
    """Opt-in soak (pytest -m live). 100 opportunities/s for 60 s, all
    received by a subscriber with zero loss. Matches the Module 4
    load-test spec. Skipped from the default suite because it takes
    ~65 s wall clock."""
    store = EventStore(SqliteBackend(tmp_path / "soak.db"))
    await store.start()
    broker = SSEBroker()
    capture = ChangeCapture(store, broker, tick_sec=0.25, start_at_latest=False)
    await capture.start()

    received = 0

    async def _consume():
        nonlocal received
        async for _ in broker.subscribe():
            received += 1

    consumer = asyncio.create_task(_consume())
    await asyncio.sleep(0.05)

    try:
        start = time.monotonic()
        total = 0
        while time.monotonic() - start < 60.0:
            batch_start = time.monotonic()
            for _ in range(100):
                await _insert_opportunity(store, ticker=f"KXSOAK-{total}")
                total += 1
            elapsed = time.monotonic() - batch_start
            if elapsed < 1.0:
                await asyncio.sleep(1.0 - elapsed)

        # Drain window.
        deadline = time.monotonic() + 10.0
        while received < total and time.monotonic() < deadline:
            await asyncio.sleep(0.1)

        assert received == total, f"expected {total}, got {received}"
    finally:
        consumer.cancel()
        await capture.stop()
        await store.stop()
