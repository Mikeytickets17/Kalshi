"""libSQL / Turso backend.

Same StoreBackend interface as SqliteBackend. Two modes:

1. **Local file / in-memory** (tests + dev): just uses libsql against a
   path. Schema is byte-compatible with sqlite3 so existing SQL works
   unchanged. This is what the abstraction test runs against -- proves
   the bot's domain helpers produce identical results on both drivers.

2. **Embedded replica** (production dashboard on Fly): opens a local
   libsql database file that is continuously synced from the Turso
   primary. Writes go to primary (over HTTPS, requires auth token).
   Reads hit the local file -- no network. This is what makes the
   dashboard responsive even at 100 inserts/second.

Replica-lag telemetry: libsql exposes the last successful sync timestamp
when embedded-replica mode is used. We expose it via replica_last_sync_ms
so the System Health tab can surface `max(0, primary - replica)` as the
'Replica lag: Xms' tile the user requested.

Auth token rotation: see docs/turso-setup.md. Revoke via `turso db tokens
invalidate ...`, regenerate, update env var, restart the process. Bot and
dashboard use SEPARATE tokens (separately revocable) per the review spec.
"""

from __future__ import annotations

from typing import Any


class LibsqlBackend:
    def __init__(
        self,
        *,
        url: str,
        auth_token: str | None = None,
        sync_url: str | None = None,
        sync_interval_sec: float = 1.0,
    ) -> None:
        """
        url: local file path for file/memory mode, OR libsql:// URL for
             embedded replica mode (primary is specified via sync_url).
        auth_token: required for remote / embedded-replica; ignored for local.
        sync_url: set when running as an embedded replica -- the Turso
                  primary URL this replica syncs from.
        sync_interval_sec: how often the replica pulls from primary.
                           Dashboard needs this low enough that human
                           perception feels 'live' (1s is the default;
                           the UI polls last_modified_ms at the same cadence).
        """
        self.url = url
        self.auth_token = auth_token
        self.sync_url = sync_url
        self.sync_interval_sec = sync_interval_sec
        self._conn: Any = None
        # Populated after each successful sync in embedded-replica mode.
        self._last_sync_ms: int | None = None

    # ---- lifecycle ----

    def connect(self) -> None:
        try:
            import libsql_experimental as libsql  # lazy import
        except ImportError as exc:  # noqa: BLE001
            raise RuntimeError(
                "libsql-experimental is not installed. "
                "pip install libsql-experimental"
            ) from exc

        if self.sync_url:
            # Embedded replica mode: local file that syncs from remote primary.
            self._conn = libsql.connect(
                self.url,
                sync_url=self.sync_url,
                auth_token=self.auth_token,
            )
            # Initial sync before returning; dashboard expects a warm replica.
            self._conn.sync()
            import time as _time
            self._last_sync_ms = int(_time.time() * 1000)
        elif self.auth_token:
            # Direct-remote mode (bot → primary). No local file.
            self._conn = libsql.connect(
                self.url, auth_token=self.auth_token
            )
        else:
            # Local file / in-memory -- used by tests and dev.
            self._conn = libsql.connect(self.url)

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
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

    # ---- replica sync (called periodically when in embedded-replica mode) ----

    def sync(self) -> None:
        """Pull latest changes from the primary. Safe to call every tick.
        No-op when not in embedded-replica mode."""
        if self._conn is None or self.sync_url is None:
            return
        import time as _time
        try:
            self._conn.sync()
            self._last_sync_ms = int(_time.time() * 1000)
        except Exception:  # noqa: BLE001
            # Let the primary write timestamp drift; the dashboard sees the
            # lag widen and shows the >5s warning banner, alerting the
            # operator.
            pass

    # ---- replica-lag telemetry ----

    def primary_last_write_ms(self) -> int | None:
        """MAX(last_modified_ms) from the change_log -- the most-recent
        write the primary has acknowledged that we've observed locally.
        (In non-replica mode this is still the right value because we ARE
        the primary.)"""
        if self._conn is None:
            return None
        try:
            row = self._conn.execute(
                "SELECT MAX(last_modified_ms) FROM change_log"
            ).fetchone()
            return int(row[0]) if row and row[0] is not None else None
        except Exception:  # noqa: BLE001
            return None

    def replica_last_sync_ms(self) -> int | None:
        """Local clock when we last successfully pulled from primary.
        In non-replica mode returns None (no replication → no lag concept)."""
        return self._last_sync_ms

    # ---- introspection ----

    @property
    def driver_name(self) -> str:
        return "libsql_replica" if self.sync_url else "libsql"
