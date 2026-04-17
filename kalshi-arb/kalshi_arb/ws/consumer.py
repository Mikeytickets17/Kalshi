"""Sharded WebSocket consumer.

Architecture
------------
- The universe selector hands us N tickers (dynamic, usually 400-600).
- We split them into shards of WS_MAX_TICKERS_PER_CONN and spawn one
  pykalshi AsyncFeed per shard.
- Each feed subscribes to orderbook_delta + ticker_v2 + trade for its
  shard's tickers.
- Incoming messages are:
    1. Stamped with receive_ms
    2. Validated against last_seq[ticker] — on gap, request resnapshot
    3. Pushed to the store as a raw event
    4. Dispatched to in-process subscribers (scanner)
- On disconnect, AsyncFeed reconnects internally. We additionally enforce a
  hard staleness watchdog: if a shard goes >60s without any message, the
  supervisor kills + respawns it.

Resilience knobs
----------------
- Reconnect uses AsyncFeed's built-in logic (exponential backoff).
- Sequence gap detection triggers a REST resnapshot via RestClient.
- Shard supervisor restarts dead shards without taking down the whole feed.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable

from .. import clock, log
from ..rest.client import RestClient
from ..store.db import EventStore

_log = log.get("ws.consumer")

# If a shard sees no messages for this long, the supervisor kills + respawns it.
SHARD_STALENESS_SEC = 60.0

# How often the supervisor loop checks shard health.
SUPERVISOR_TICK_SEC = 5.0

# Bucket size for ws_metrics rows (60s).
METRIC_BUCKET_MS = 60_000


@dataclass
class ShardHealth:
    shard_id: int
    tickers: list[str]
    started_at_ms: int
    last_msg_ms: int = 0
    msg_count: int = 0
    gap_count: int = 0
    reconnect_count: int = 0
    alive: bool = False


MessageHandler = Callable[[dict[str, Any]], None]


@dataclass
class ShardedWS:
    rest: RestClient
    store: EventStore
    max_tickers_per_conn: int
    on_orderbook_delta: MessageHandler | None = None
    on_trade: MessageHandler | None = None
    on_ticker: MessageHandler | None = None

    _shards: dict[int, ShardHealth] = field(default_factory=dict)
    _tasks: dict[int, asyncio.Task[None]] = field(default_factory=dict)
    _seq_by_ticker: dict[str, int] = field(default_factory=dict)
    _msg_count_bucket: dict[tuple[int, str], int] = field(default_factory=lambda: defaultdict(int))
    _gap_count_bucket: dict[tuple[int, str], int] = field(default_factory=lambda: defaultdict(int))
    _last_msg_ms_by_ticker: dict[str, int] = field(default_factory=dict)
    _supervisor_task: asyncio.Task[None] | None = None
    _metric_flush_task: asyncio.Task[None] | None = None
    _stopped: bool = False

    async def start(self, tickers: list[str]) -> None:
        if not tickers:
            _log.warning("ws.start_empty_universe")
            return
        shards = self._split(tickers, self.max_tickers_per_conn)
        for i, shard_tickers in enumerate(shards):
            self._spawn_shard(i, shard_tickers)
        self._supervisor_task = asyncio.create_task(self._supervise(), name="ws-supervisor")
        self._metric_flush_task = asyncio.create_task(self._flush_metrics(), name="ws-metrics")
        _log.info(
            "ws.started",
            shard_count=len(shards),
            total_tickers=len(tickers),
            max_per_shard=self.max_tickers_per_conn,
        )

    async def stop(self) -> None:
        self._stopped = True
        for t in list(self._tasks.values()):
            t.cancel()
        for t in [self._supervisor_task, self._metric_flush_task]:
            if t:
                t.cancel()
        for t in list(self._tasks.values()):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._flush_metric_buckets()
        _log.info("ws.stopped")

    def health(self) -> list[ShardHealth]:
        return list(self._shards.values())

    # --- internal ---

    @staticmethod
    def _split(items: list[str], size: int) -> list[list[str]]:
        return [items[i : i + size] for i in range(0, len(items), size)]

    def _spawn_shard(self, shard_id: int, tickers: list[str]) -> None:
        self._shards[shard_id] = ShardHealth(
            shard_id=shard_id,
            tickers=list(tickers),
            started_at_ms=clock.now_ms(),
        )
        self._tasks[shard_id] = asyncio.create_task(
            self._run_shard(shard_id, tickers), name=f"ws-shard-{shard_id}"
        )

    async def _run_shard(self, shard_id: int, tickers: list[str]) -> None:
        """One shard = one AsyncFeed subscribed to multiple channels."""
        health = self._shards[shard_id]
        try:
            feed = self.rest.underlying.feed()  # pykalshi sync feed
        except Exception as exc:  # noqa: BLE001
            _log.error("ws.feed_create_failed", shard_id=shard_id, error=str(exc))
            health.alive = False
            return

        try:
            async with feed as f:
                # Register handlers BEFORE subscribing so nothing is dropped.
                @f.on("orderbook_delta")
                def _h_delta(msg: Any) -> None:
                    self._handle_delta(shard_id, msg)

                @f.on("orderbook_snapshot")
                def _h_snap(msg: Any) -> None:
                    self._handle_snapshot(shard_id, msg)

                @f.on("trade")
                def _h_trade(msg: Any) -> None:
                    self._handle_trade(shard_id, msg)

                @f.on("ticker_v2")
                def _h_ticker(msg: Any) -> None:
                    self._handle_ticker(shard_id, msg)

                for channel in ("orderbook_delta", "trade", "ticker_v2"):
                    try:
                        f.subscribe(channel, market_tickers=tickers)
                    except Exception as exc:  # noqa: BLE001
                        _log.error(
                            "ws.subscribe_failed",
                            shard_id=shard_id,
                            channel=channel,
                            error=str(exc),
                        )

                health.alive = True
                _log.info("ws.shard_connected", shard_id=shard_id, tickers=len(tickers))

                # Drain — AsyncFeed is an async iterator that yields messages;
                # handlers are called automatically. We just need to keep the
                # coroutine alive.
                async for _ in f:
                    if self._stopped:
                        break
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            _log.error("ws.shard_crashed", shard_id=shard_id, error=str(exc))
        finally:
            health.alive = False

    async def _supervise(self) -> None:
        while not self._stopped:
            await asyncio.sleep(SUPERVISOR_TICK_SEC)
            now_ms = clock.now_ms()
            for shard_id, health in list(self._shards.items()):
                task = self._tasks.get(shard_id)
                stale = (
                    health.last_msg_ms > 0
                    and (now_ms - health.last_msg_ms) / 1000 > SHARD_STALENESS_SEC
                )
                dead = task is None or task.done()
                if stale or dead:
                    _log.warning(
                        "ws.shard_respawn",
                        shard_id=shard_id,
                        stale=stale,
                        dead=dead,
                        last_msg_age_sec=(now_ms - health.last_msg_ms) / 1000,
                    )
                    health.reconnect_count += 1
                    if task and not task.done():
                        task.cancel()
                    self._spawn_shard(shard_id, health.tickers)

    def _handle_delta(self, shard_id: int, msg: Any) -> None:
        ticker = _get(msg, "market_ticker")
        seq = int(_get(msg, "seq", 0) or 0)
        price = int(_get(msg, "price", 0) or 0)
        delta = int(_get(msg, "delta", 0) or 0)
        side = str(_get(msg, "side", "") or "").lower()
        if not ticker or side not in ("yes", "no"):
            return

        self._record_msg(shard_id, ticker)

        # Gap detection
        last = self._seq_by_ticker.get(ticker, 0)
        if last and seq > last + 1:
            self._gap_count_bucket[(_bucket(), ticker)] += 1
            _log.warning(
                "ws.seq_gap",
                ticker=ticker,
                last_seq=last,
                new_seq=seq,
                gap=seq - last - 1,
            )
            self._shards[shard_id].gap_count += 1
            # Request a REST resnapshot out-of-band so we don't block the feed
            asyncio.create_task(self._resnap(ticker), name=f"resnap-{ticker}")
        self._seq_by_ticker[ticker] = seq

        self.store.record_orderbook_event(ticker, seq, side, price, delta, "delta")
        if self.on_orderbook_delta:
            self.on_orderbook_delta(
                {"ticker": ticker, "seq": seq, "side": side, "price": price, "delta": delta}
            )

    def _handle_snapshot(self, shard_id: int, msg: Any) -> None:
        ticker = _get(msg, "market_ticker")
        seq = int(_get(msg, "seq", 0) or 0)
        if not ticker:
            return
        self._record_msg(shard_id, ticker)
        self._seq_by_ticker[ticker] = seq
        # Persist snapshot as msgpack-encoded levels.
        yes_levels = _get(msg, "yes", []) or []
        no_levels = _get(msg, "no", []) or []
        try:
            import msgpack

            self.store.submit(
                _snapshot_job(ticker, clock.now_ms(), seq, yes_levels, no_levels, msgpack)
            )
        except Exception as exc:  # noqa: BLE001
            _log.error("ws.snapshot_persist_failed", ticker=ticker, error=str(exc))

    def _handle_trade(self, shard_id: int, msg: Any) -> None:
        ticker = _get(msg, "market_ticker")
        if not ticker:
            return
        self._record_msg(shard_id, ticker)
        price = int(_get(msg, "price", 0) or 0)
        count = int(_get(msg, "count", 0) or 0)
        taker_side = str(_get(msg, "taker_side", "") or "").lower()
        if taker_side not in ("yes", "no") or count <= 0:
            return
        from ..store.db import WriteJob

        self.store.submit(
            WriteJob(
                "INSERT INTO trades(ticker, ts_ms, price, count, taker_side) VALUES(?,?,?,?,?)",
                (ticker, clock.now_ms(), price, count, taker_side),
            )
        )
        if self.on_trade:
            self.on_trade(
                {"ticker": ticker, "price": price, "count": count, "taker_side": taker_side}
            )

    def _handle_ticker(self, shard_id: int, msg: Any) -> None:
        ticker = _get(msg, "market_ticker")
        if not ticker:
            return
        self._record_msg(shard_id, ticker)
        if self.on_ticker:
            self.on_ticker({"ticker": ticker, "raw": msg})

    def _record_msg(self, shard_id: int, ticker: str) -> None:
        now_ms = clock.now_ms()
        self._shards[shard_id].last_msg_ms = now_ms
        self._shards[shard_id].msg_count += 1
        self._last_msg_ms_by_ticker[ticker] = now_ms
        self._msg_count_bucket[(_bucket(), ticker)] += 1

    async def _resnap(self, ticker: str) -> None:
        try:
            book = await asyncio.to_thread(self.rest.get_orderbook, ticker)
        except Exception as exc:  # noqa: BLE001
            _log.error("ws.resnap_failed", ticker=ticker, error=str(exc))
            return
        from ..store.db import WriteJob

        try:
            import msgpack

            yes_levels = book.get("yes", []) or []
            no_levels = book.get("no", []) or []
            self.store.submit(
                WriteJob(
                    "INSERT INTO orderbook_snapshots(ticker, ts_ms, seq, yes_levels, no_levels)"
                    " VALUES(?,?,?,?,?)",
                    (
                        ticker,
                        clock.now_ms(),
                        self._seq_by_ticker.get(ticker, 0),
                        msgpack.packb(yes_levels),
                        msgpack.packb(no_levels),
                    ),
                )
            )
        except Exception as exc:  # noqa: BLE001
            _log.error("ws.resnap_persist_failed", ticker=ticker, error=str(exc))

    async def _flush_metrics(self) -> None:
        """Every bucket (60s), flush per-ticker message counts to ws_metrics."""
        while not self._stopped:
            await asyncio.sleep(METRIC_BUCKET_MS / 1000)
            self._flush_metric_buckets()

    def _flush_metric_buckets(self) -> None:
        if not self._msg_count_bucket and not self._gap_count_bucket:
            return
        buckets_to_flush = set(self._msg_count_bucket.keys()) | set(self._gap_count_bucket.keys())
        current = _bucket()
        for bucket, ticker in list(buckets_to_flush):
            if bucket == current:
                continue  # still filling; flush next tick
            msg = self._msg_count_bucket.pop((bucket, ticker), 0)
            gap = self._gap_count_bucket.pop((bucket, ticker), 0)
            self.store.record_ws_metric(
                bucket_ts_ms=bucket,
                ticker=ticker,
                msg_count=msg,
                gap_count=gap,
                last_seq=self._seq_by_ticker.get(ticker),
                last_msg_ms=self._last_msg_ms_by_ticker.get(ticker),
            )


def _bucket() -> int:
    now = clock.now_ms()
    return now - (now % METRIC_BUCKET_MS)


def _get(msg: Any, key: str, default: Any = None) -> Any:
    """Access a field from a pykalshi message object or dict."""
    if msg is None:
        return default
    if isinstance(msg, dict):
        return msg.get(key, default)
    return getattr(msg, key, default)


def _snapshot_job(ticker: str, ts_ms: int, seq: int, yes: list[Any], no: list[Any], msgpack: Any):
    from ..store.db import WriteJob

    return WriteJob(
        "INSERT INTO orderbook_snapshots(ticker, ts_ms, seq, yes_levels, no_levels)"
        " VALUES(?,?,?,?,?)",
        (ticker, ts_ms, seq, msgpack.packb(yes), msgpack.packb(no)),
    )
