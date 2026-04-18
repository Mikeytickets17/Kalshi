"""Async event store.

Public interface the rest of the bot depends on. Backend-agnostic --
talks to a StoreBackend protocol (sqlite_backend.SqliteBackend OR
libsql_backend.LibsqlBackend) without caring which is underneath.

Design invariants:
- Single writer coroutine drains a bounded queue. Producers (WS consumer,
  scanner, executor) push writes and never block on the storage driver.
- Queue overflow drops oldest-first and logs -- never crash the hot path.
- Every domain write ALSO appends a change_log row with the same ts so
  the dashboard's 1-second poll sees every new event deterministically.
- Reads are synchronous; the dashboard thread calls them directly.

This file MUST NOT import sqlite3, libsql, libsql_experimental, or any
other driver directly. All driver access goes through self._backend.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import clock, log
from .backend import StoreBackend
from .sqlite_backend import SqliteBackend

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
    # When provided, a change_log row is written in the SAME transaction as
    # the main write. None = no change_log entry (e.g. ws_metrics, too noisy).
    change_entity_type: str | None = None
    change_entity_id: int | None = None
    change_payload: str | None = None


class EventStore:
    """Backend-agnostic event store. Pass either a Path (local SQLite) or
    any object implementing StoreBackend."""

    def __init__(self, path_or_backend: Path | str | StoreBackend) -> None:
        if isinstance(path_or_backend, (Path, str)):
            self._backend: StoreBackend = SqliteBackend(path_or_backend)
        else:
            self._backend = path_or_backend
        # Queue is bound to the running event loop, so create it lazily in
        # start(). Constructing asyncio.Queue at __init__ would bind it to
        # whichever loop happened to exist at import time -- that's a
        # footgun in tests that use multiple asyncio.run() calls.
        self._queue: asyncio.Queue[WriteJob] | None = None
        self._writer_task: asyncio.Task[None] | None = None
        self._closed = False
        self._connected = False
        self._stats_written = 0
        self._stats_dropped = 0

    @property
    def backend(self) -> StoreBackend:
        """For observability -- e.g. System Health tab reading replica lag.
        Callers must only read; do not poke at connection internals."""
        return self._backend

    def connect(self) -> None:
        if self._connected:
            return
        self._backend.connect()
        with SCHEMA_PATH.open() as f:
            self._backend.executescript(f.read())
        self._connected = True
        _log.info("store.connected", driver=self._backend.driver_name)

    async def start(self) -> None:
        # Lazy connect -- allows tests to construct an EventStore without
        # immediately opening a file.
        if not self._connected:
            self.connect()
        if self._queue is None:
            self._queue = asyncio.Queue(maxsize=WRITE_QUEUE_MAX)
        if self._writer_task is None or self._writer_task.done():
            self._closed = False
            self._writer_task = asyncio.create_task(
                self._writer_loop(), name="store-writer"
            )

    async def stop(self) -> None:
        self._closed = True
        if self._queue is not None:
            await self._queue.join()
        if self._writer_task:
            self._writer_task.cancel()
            try:
                await self._writer_task
            except asyncio.CancelledError:
                pass
        self._backend.close()
        self._connected = False
        _log.info(
            "store.closed", written=self._stats_written, dropped=self._stats_dropped
        )

    def submit(self, job: WriteJob) -> None:
        """Non-blocking submit. Drops job with a WARN if the queue is full
        or the store hasn't been started yet (a test/caller misuse)."""
        if self._queue is None:
            self._stats_dropped += 1
            _log.warning("store.submit_before_start", sql=job.sql[:80])
            return
        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull:
            self._stats_dropped += 1
            if self._stats_dropped % 1000 == 1:
                _log.warning("store.queue_full", dropped_total=self._stats_dropped)

    async def _writer_loop(self) -> None:
        assert self._queue is not None, "writer loop started before queue init"
        while not self._closed or not self._queue.empty():
            try:
                job = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except TimeoutError:
                continue
            try:
                if job.many:
                    self._backend.executemany(job.sql, job.params)  # type: ignore[arg-type]
                else:
                    self._backend.execute(job.sql, job.params)  # type: ignore[arg-type]
                if job.change_entity_type is not None:
                    self._backend.execute(
                        "INSERT INTO change_log(entity_type, entity_id, last_modified_ms, payload)"
                        " VALUES(?,?,?,?)",
                        (
                            job.change_entity_type,
                            job.change_entity_id,
                            clock.now_ms(),
                            job.change_payload,
                        ),
                    )
                self._stats_written += 1
            except Exception as exc:  # noqa: BLE001
                _log.error("store.write_failed", error=str(exc), sql=job.sql[:80])
            finally:
                self._queue.task_done()

    # --- Synchronous reads (dashboard) ---

    def read_one(self, sql: str, params: tuple[Any, ...] = ()) -> tuple[Any, ...] | None:
        return self._backend.fetch_one(sql, params)

    def read_many(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        return self._backend.fetch_many(sql, params)

    # --- Change-capture helpers used by the dashboard SSE endpoint ---

    def changes_since(
        self, since_id: int, *, limit: int = 500, entity_type: str | None = None
    ) -> list[tuple[int, str, int | None, int, str | None]]:
        """Return (id, entity_type, entity_id, last_modified_ms, payload) rows
        with id > since_id. Dashboard calls this every second to fan out
        SSE events to connected browsers."""
        if entity_type is not None:
            return self.read_many(
                "SELECT id, entity_type, entity_id, last_modified_ms, payload"
                " FROM change_log WHERE id > ? AND entity_type = ?"
                " ORDER BY id ASC LIMIT ?",
                (since_id, entity_type, limit),
            )
        return self.read_many(
            "SELECT id, entity_type, entity_id, last_modified_ms, payload"
            " FROM change_log WHERE id > ?"
            " ORDER BY id ASC LIMIT ?",
            (since_id, limit),
        )

    def change_log_count(self) -> int:
        """Total rows currently visible in change_log on this connection.
        Used by /healthz so a separate verifier process can compare its
        own count to the dashboard's, isolating WAL-visibility issues
        from path-mismatch issues."""
        rows = self.read_many("SELECT COUNT(*) FROM change_log", ())
        return int(rows[0][0]) if rows else 0

    def replica_lag_ms(self) -> int | None:
        """Exposed on the System Health tab. See backend.py docstring."""
        primary = self._backend.primary_last_write_ms()
        replica = self._backend.replica_last_sync_ms()
        if primary is None or replica is None:
            return None
        return max(0, primary - replica)

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
        # orderbook_events are too high-volume to stream to the dashboard;
        # change_log is intentionally omitted for this entity.
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
        # High-frequency internal telemetry; doesn't hit change_log.
        self.submit(
            WriteJob(
                "INSERT INTO ws_metrics(bucket_ts_ms, ticker, msg_count, gap_count, last_seq, last_msg_ms)"
                " VALUES(?,?,?,?,?,?)",
                (bucket_ts_ms, ticker, msg_count, gap_count, last_seq, last_msg_ms),
            )
        )

    def record_opportunity(
        self,
        *,
        ticker: str,
        ts_ms: int,
        yes_ask_cents: int,
        yes_ask_qty: int,
        no_ask_cents: int,
        no_ask_qty: int,
        sum_cents: int,
        est_fees_cents: int,
        slippage_buffer: int,
        net_edge_cents: float,
        max_size_liquidity: int,
        kelly_size: int,
        hard_cap_size: int,
        final_size: int,
        decision: str,
        rejection_reason: str | None = None,
    ) -> None:
        """Every scanner decision -- emit or skip -- lands here AND hits
        the change_log so the dashboard's Opportunities tab streams it."""
        self.submit(
            WriteJob(
                """INSERT INTO opportunities_detected(
                    ticker, ts_ms, yes_ask_cents, yes_ask_qty,
                    no_ask_cents, no_ask_qty, sum_cents,
                    est_fees_cents, slippage_buffer, net_edge_cents,
                    max_size_liquidity, kelly_size, hard_cap_size,
                    final_size, decision, rejection_reason)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    ticker, ts_ms, yes_ask_cents, yes_ask_qty,
                    no_ask_cents, no_ask_qty, sum_cents,
                    est_fees_cents, slippage_buffer, net_edge_cents,
                    max_size_liquidity, kelly_size, hard_cap_size,
                    final_size, decision, rejection_reason,
                ),
                change_entity_type="opportunity",
                change_payload=None,
            )
        )

    def record_kill_switch_change(self, tripped: bool, reason: str) -> None:
        # Small event, fits in the payload directly.
        import json
        self.submit(
            WriteJob(
                "INSERT INTO change_log(entity_type, entity_id, last_modified_ms, payload)"
                " VALUES(?,?,?,?)",
                (
                    "kill_switch",
                    None,
                    clock.now_ms(),
                    json.dumps({"tripped": tripped, "reason": reason}),
                ),
            )
        )

    def record_order_placed(
        self,
        *,
        client_order_id: str,
        kalshi_order_id: str | None,
        opportunity_id: int,
        ticker: str,
        side: str,
        action: str,
        type_: str,
        limit_price: int,
        count: int,
        placed_ok: bool,
        error: str | None,
    ) -> None:
        """Both legs of an arb are recorded here; matched via opportunity_id."""
        import json
        self.submit(
            WriteJob(
                """INSERT INTO orders_placed(
                    client_order_id, kalshi_order_id, opportunity_id,
                    ticker, side, action, type, limit_price, count,
                    placed_ts_ms, placed_ok, error)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    client_order_id, kalshi_order_id, opportunity_id,
                    ticker, side, action, type_, limit_price, count,
                    clock.now_ms(), 1 if placed_ok else 0, error,
                ),
                change_entity_type="order_placed",
                change_entity_id=opportunity_id,
                change_payload=json.dumps({
                    "client_order_id": client_order_id,
                    "ticker": ticker,
                    "side": side,
                    "count": count,
                    "limit_price": limit_price,
                    "placed_ok": placed_ok,
                }),
            )
        )

    def record_order_filled(
        self,
        *,
        client_order_id: str,
        filled_price: int,
        filled_count: int,
        fees_cents: int = 0,
    ) -> None:
        import json
        self.submit(
            WriteJob(
                """INSERT INTO orders_filled(
                    client_order_id, filled_ts_ms, filled_price, filled_count, fees_cents)
                   VALUES(?,?,?,?,?)""",
                (
                    client_order_id, clock.now_ms(), filled_price,
                    filled_count, fees_cents,
                ),
                change_entity_type="order_filled",
                change_payload=json.dumps({
                    "client_order_id": client_order_id,
                    "filled_price": filled_price,
                    "filled_count": filled_count,
                    "fees_cents": fees_cents,
                }),
            )
        )

    def record_pnl_realized(
        self,
        *,
        opportunity_id: int,
        yes_pnl_cents: int,
        no_pnl_cents: int,
        fees_cents: int,
        net_cents: int,
        note: str | None = None,
    ) -> None:
        import json
        self.submit(
            WriteJob(
                """INSERT INTO pnl_realized(
                    opportunity_id, settled_ts_ms, yes_pnl_cents, no_pnl_cents,
                    fees_cents, net_cents, note)
                   VALUES(?,?,?,?,?,?,?)""",
                (
                    opportunity_id, clock.now_ms(), yes_pnl_cents, no_pnl_cents,
                    fees_cents, net_cents, note,
                ),
                change_entity_type="pnl_realized",
                change_entity_id=opportunity_id,
                change_payload=json.dumps({
                    "opportunity_id": opportunity_id,
                    "net_cents": net_cents,
                }),
            )
        )

    def record_degraded_event(self, kind: str, detail: str) -> None:
        """Degraded-mode notifications: WS reconnect storms, sequential
        portfolio reads, slow probe results -- anything that's not an
        outage but warrants operator attention. Streams to the dashboard
        via change_log only (no separate table; we never need to query
        them out-of-band)."""
        import json
        self.submit(
            WriteJob(
                "INSERT INTO change_log(entity_type, entity_id, last_modified_ms, payload)"
                " VALUES(?,?,?,?)",
                (
                    "degraded",
                    None,
                    clock.now_ms(),
                    json.dumps({"kind": kind, "detail": detail}),
                ),
            )
        )

    def record_probe_run(
        self,
        *,
        env_tag: str,
        probe_name: str,
        status: str,
        latency_ms: int | None,
        error: str | None = None,
    ) -> None:
        """Probe results land here from the separate probe process so the
        System Health tab can show last-run status per probe per env."""
        import json
        self.submit(
            WriteJob(
                """INSERT INTO probe_runs(ts_ms, env_tag, probe_name, status, latency_ms, error)
                   VALUES(?,?,?,?,?,?)""",
                (
                    clock.now_ms(), env_tag, probe_name, status,
                    latency_ms, error,
                ),
                change_entity_type="probe_run",
                change_payload=json.dumps({
                    "env_tag": env_tag,
                    "probe_name": probe_name,
                    "status": status,
                }),
            )
        )

    def stats(self) -> dict[str, int]:
        return {
            "queue_depth": self._queue.qsize() if self._queue is not None else 0,
            "written_total": self._stats_written,
            "dropped_total": self._stats_dropped,
        }
