"""SSE broker + change-capture poller.

Architecture (single-process, single-operator):

    ┌──────────────────────┐
    │ ChangeCapture        │  background task
    │  polls EventStore    │  1-second cadence
    │  since_id bookmark   │
    └──────────┬───────────┘
               │ publish(Change)
               ▼
    ┌──────────────────────┐
    │ SSEBroker (singleton)│
    │  set of per-client   │
    │  asyncio.Queues      │
    └──────────┬───────────┘
               │ pulled by each /events/stream handler
               ▼
    ┌──────────────────────┐
    │ text/event-stream    │  one generator per connected browser
    └──────────────────────┘

Design notes:
 * Bounded per-client queue (QUEUE_SIZE). If a slow client fills up, we
   close its queue with a sentinel -- the generator cleans up and the
   browser auto-reconnects via EventSource. Never block the broadcaster.
 * Broker is cheap to create; one instance is owned by the FastAPI app
   via app.state.broker (wired in app.py).
 * Each Change carries an integer id (change_log.id). Clients store this
   as EventSource's 'lastEventId' and send it back via the
   Last-Event-ID header on reconnect -- broker then serves from the DB
   starting after that id to fill the gap with zero loss.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from .. import log
from ..store import EventStore

_log = log.get("dashboard.sse")


QUEUE_SIZE = 1024
# If queue is full when broadcaster wants to enqueue, we close it with
# this sentinel so the stream generator exits cleanly and the client
# reconnects. Alternative (blocking the broadcaster) would stall every
# other client too.
_CLOSE_SENTINEL: object = object()


@dataclass(frozen=True)
class Change:
    """One row out of change_log, normalized for SSE wire format."""

    id: int
    entity_type: str
    entity_id: int | None
    ts_ms: int
    payload: str | None

    @classmethod
    def from_row(cls, row: tuple[Any, ...]) -> "Change":
        return cls(
            id=int(row[0]),
            entity_type=str(row[1]),
            entity_id=int(row[2]) if row[2] is not None else None,
            ts_ms=int(row[3]),
            payload=row[4] if row[4] is None else str(row[4]),
        )

    def as_sse_event(self) -> str:
        """Serialize to the SSE wire format (RFC-compliant).

        'event: <entity_type>' lets client code subscribe per type via
        addEventListener('opportunity', ...). 'id: <change.id>' is what
        EventSource echoes in Last-Event-ID on reconnect."""
        data = {
            "id": self.id,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "ts_ms": self.ts_ms,
            "payload": self.payload,
        }
        return (
            f"event: {self.entity_type}\n"
            f"id: {self.id}\n"
            f"data: {json.dumps(data, separators=(',', ':'))}\n\n"
        )


@dataclass
class SSEBroker:
    """In-process fan-out. Producers call publish(); consumers call
    subscribe() to get an async iterator of Change objects."""

    _subscribers: set[asyncio.Queue] = field(default_factory=set)
    _published_total: int = 0
    _dropped_clients: int = 0

    def publish(self, change: Change) -> None:
        """Non-blocking. Each subscriber's queue receives the change; if
        a subscriber's queue is full we evict that subscriber rather
        than stalling the pipeline."""
        self._published_total += 1
        dead: list[asyncio.Queue] = []
        for q in self._subscribers:
            try:
                q.put_nowait(change)
            except asyncio.QueueFull:
                dead.append(q)
                self._dropped_clients += 1
        for q in dead:
            self._subscribers.discard(q)
            try:
                q.put_nowait(_CLOSE_SENTINEL)  # type: ignore[arg-type]
            except asyncio.QueueFull:
                pass

    def add_subscriber(self) -> asyncio.Queue:
        """Lower-level primitive. Create a Queue, register it, return it.
        Caller is responsible for calling remove_subscriber() in a
        finally block. Used by the SSE handler which needs to race
        get() against a disconnect-check timer (impossible when the
        queue is hidden inside a generator)."""
        q: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)
        self._subscribers.add(q)
        return q

    def remove_subscriber(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    async def subscribe(self) -> AsyncIterator[Change]:
        """High-level generator. Suitable for tests and any consumer
        that can rely on the process exiting cleanly; the SSE HTTP
        handler uses add_subscriber() directly."""
        q = self.add_subscriber()
        try:
            while True:
                item = await q.get()
                if item is _CLOSE_SENTINEL:
                    return
                yield item  # type: ignore[misc]
        finally:
            self.remove_subscriber(q)

    def stats(self) -> dict[str, int]:
        return {
            "subscribers": len(self._subscribers),
            "published_total": self._published_total,
            "dropped_clients": self._dropped_clients,
        }


class ChangeCapture:
    """Background task: every tick_sec seconds, reads new rows from
    change_log and publishes them to the broker. Owns the since_id
    bookmark. Resilient to transient DB errors (logs and retries)."""

    def __init__(
        self,
        store: EventStore,
        broker: SSEBroker,
        *,
        tick_sec: float = 1.0,
        batch_limit: int = 500,
        start_at_latest: bool = True,
    ) -> None:
        self.store = store
        self.broker = broker
        self.tick_sec = tick_sec
        self.batch_limit = batch_limit
        self._since_id = 0
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._start_at_latest = start_at_latest

    async def start(self) -> None:
        if self._start_at_latest:
            # Initialize bookmark so we don't re-broadcast the entire
            # backlog on startup -- streams are live from now forward.
            row = self.store.read_one("SELECT COALESCE(MAX(id), 0) FROM change_log")
            self._since_id = int(row[0]) if row else 0
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="change-capture")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                rows = self.store.changes_since(
                    self._since_id, limit=self.batch_limit
                )
            except Exception as exc:  # noqa: BLE001
                _log.warning("change_capture.read_failed", error=str(exc))
                rows = []
            if rows:
                for row in rows:
                    change = Change.from_row(row)
                    self.broker.publish(change)
                self._since_id = int(rows[-1][0])
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.tick_sec)
            except TimeoutError:
                pass

    @property
    def since_id(self) -> int:
        return self._since_id
