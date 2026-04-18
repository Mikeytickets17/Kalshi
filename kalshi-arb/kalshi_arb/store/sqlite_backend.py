"""Local SQLite backend.

Wraps sqlite3 with the StoreBackend interface. Behaves exactly like the
previous hand-rolled EventStore did -- WAL mode, same PRAGMAs, same
single-writer-at-a-time discipline. The only observable change is that
the driver is now hidden behind an abstraction.

For local dev, tests, and the bot running on laptop during paper phase.
Not used on the Fly machine (that one uses LibsqlBackend with embedded
replica).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


class SqliteBackend:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._conn: sqlite3.Connection | None = None

    # ---- lifecycle ----

    def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.path), isolation_level=None, check_same_thread=False
        )

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ---- write path ----

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        assert self._conn is not None, "backend not connected"
        self._conn.execute(sql, params)

    def executemany(self, sql: str, params: list[tuple[Any, ...]]) -> None:
        assert self._conn is not None, "backend not connected"
        self._conn.executemany(sql, params)

    def executescript(self, sql: str) -> None:
        assert self._conn is not None, "backend not connected"
        self._conn.executescript(sql)

    # ---- read path ----

    def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> tuple[Any, ...] | None:
        assert self._conn is not None, "backend not connected"
        row = self._conn.execute(sql, params).fetchone()
        return tuple(row) if row else None

    def fetch_many(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        assert self._conn is not None, "backend not connected"
        return [tuple(r) for r in self._conn.execute(sql, params).fetchall()]

    # ---- replica-lag telemetry ----
    # Local SQLite has no replication -- primary and replica are the same
    # file. Lag is always 0. Return the highest last_modified_ms we've
    # written so the dashboard's System Health tab always has *something*
    # to display, rather than a null/hidden tile.

    def primary_last_write_ms(self) -> int | None:
        return self._max_last_modified()

    def replica_last_sync_ms(self) -> int | None:
        return self._max_last_modified()

    def _max_last_modified(self) -> int | None:
        if self._conn is None:
            return None
        # The change_log table aggregates last_modified_ms across every
        # tracked table via triggers (see schema.sql).
        try:
            row = self._conn.execute(
                "SELECT MAX(last_modified_ms) FROM change_log"
            ).fetchone()
            return int(row[0]) if row and row[0] is not None else None
        except sqlite3.Error:
            return None

    # ---- introspection ----

    @property
    def driver_name(self) -> str:
        return "sqlite"
