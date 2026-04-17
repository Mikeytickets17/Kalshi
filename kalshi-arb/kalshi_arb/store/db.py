"""Async SQLite event store.

Design:
- Single writer coroutine drains a bounded queue. All producers (WS consumer,
  scanner, executor) push writes into the queue; they never block on SQLite.
- Queue overflow drops the oldest write and emits a structured warning — we
  prefer losing a stale observability row to crashing the hot path.
- WAL mode so readers (dashboard, backtest) don't block writers.
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import clock, log

_log = log.get("store.db")

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# Queue capacity. Each event is ~200 bytes; 50k = ~10 MB buffer. Overflow
# indicates the writer is falling behind — we log and drop rather than stall.
WRITE_QUEUE_MAX = 50_000


@dataclass
class WriteJob:
    sql: str
    params: tuple[Any, ...] | list[tuple[Any, ...]] = field(default_factory=tuple)
    many: bool = False


class EventStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._conn: sqlite3.Connection | None = None
        self._queue: asyncio.Queue[WriteJob] = asyncio.Queue(maxsize=WRITE_QUEUE_MAX)
        self._writer_task: asyncio.Task[None] | None = None
        self._closed = False
        self._stats_written = 0
        self._stats_dropped = 0

    def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.path), isolation_level=None, check_same_thread=False
        )
        with SCHEMA_PATH.open() as f:
            self._conn.executescript(f.read())
        _log.info("store.connected", path=str(self.path))

    async def start(self) -> None:
        if self._conn is None:
            self.connect()
        self._writer_task = asyncio.create_task(self._writer_loop(), name="store-writer")

    async def stop(self) -> None:
        self._closed = True
        # Drain remaining
        await self._queue.join()
        if self._writer_task:
            self._writer_task.cancel()
            try:
                await self._writer_task
            except asyncio.CancelledError:
                pass
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        _log.info("store.closed", written=self._stats_written, dropped=self._stats_dropped)

    def submit(self, job: WriteJob) -> None:
        """Non-blocking submit. Drops job with a WARN if the queue is full."""
        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull:
            self._stats_dropped += 1
            if self._stats_dropped % 1000 == 1:
                _log.warning("store.queue_full", dropped_total=self._stats_dropped)

    async def _writer_loop(self) -> None:
        assert self._conn is not None
        while not self._closed or not self._queue.empty():
            try:
                job = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except TimeoutError:
                continue
            try:
                if job.many:
                    self._conn.executemany(job.sql, job.params)  # type: ignore[arg-type]
                else:
                    self._conn.execute(job.sql, job.params)  # type: ignore[arg-type]
                self._stats_written += 1
            except sqlite3.Error as exc:
                _log.error("store.write_failed", error=str(exc), sql=job.sql[:80])
            finally:
                self._queue.task_done()

    # --- High-level helpers (synchronous reads are fine — dashboard uses them) ---

    def read_one(self, sql: str, params: tuple[Any, ...] = ()) -> tuple[Any, ...] | None:
        assert self._conn is not None
        row = self._conn.execute(sql, params).fetchone()
        return tuple(row) if row else None

    def read_many(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        assert self._conn is not None
        return [tuple(r) for r in self._conn.execute(sql, params).fetchall()]

    # --- Domain helpers for hot-path writes ---

    def record_orderbook_event(
        self,
        ticker: str,
        seq: int,
        side: str,
        price: int,
        delta: int,
        kind: str = "delta",
    ) -> None:
        self.submit(
            WriteJob(
                "INSERT INTO orderbook_events(ticker, ts_ms, seq, side, price, delta, event_kind)"
                " VALUES(?,?,?,?,?,?,?)",
                (ticker, clock.now_ms(), seq, side, price, delta, kind),
            )
        )

    def upsert_market(self, m: dict[str, Any]) -> None:
        now = clock.now_ms()
        self.submit(
            WriteJob(
                """
                INSERT INTO markets(ticker, series_ticker, event_ticker, title, subtitle,
                                    category, status, open_ts_ms, close_ts_ms,
                                    first_seen_ms, last_seen_ms, excluded, excluded_reason)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(ticker) DO UPDATE SET
                    status = excluded.status,
                    last_seen_ms = excluded.last_seen_ms,
                    excluded = excluded.excluded,
                    excluded_reason = excluded.excluded_reason
                """,
                (
                    m["ticker"],
                    m.get("series_ticker") or "",
                    m.get("event_ticker"),
                    m.get("title"),
                    m.get("subtitle"),
                    m.get("category"),
                    m.get("status", "unknown"),
                    m.get("open_ts_ms"),
                    m.get("close_ts_ms"),
                    now,
                    now,
                    1 if m.get("excluded") else 0,
                    m.get("excluded_reason"),
                ),
            )
        )

    def record_ws_metric(
        self,
        bucket_ts_ms: int,
        ticker: str,
        msg_count: int,
        gap_count: int,
        last_seq: int | None,
        last_msg_ms: int | None,
    ) -> None:
        self.submit(
            WriteJob(
                "INSERT INTO ws_metrics(bucket_ts_ms, ticker, msg_count, gap_count, last_seq, last_msg_ms)"
                " VALUES(?,?,?,?,?,?)",
                (bucket_ts_ms, ticker, msg_count, gap_count, last_seq, last_msg_ms),
            )
        )

    def stats(self) -> dict[str, int]:
        return {
            "queue_depth": self._queue.qsize(),
            "written_total": self._stats_written,
            "dropped_total": self._stats_dropped,
        }
