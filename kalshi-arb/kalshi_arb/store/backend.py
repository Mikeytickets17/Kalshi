"""Storage backend abstraction.

The EventStore class is the public interface the rest of the bot talks to.
All scanner / sizer / executor code depends on EventStore's domain helper
methods; NONE of it imports sqlite3, libsql, or any driver directly.

Two concrete backends live in store/sqlite_backend.py and
store/libsql_backend.py. They implement this Protocol identically so the
bot has zero knowledge of which is in use.

This is the abstraction review-mandate from the Module 4 gate:
  "Don't leak libSQL specifics into scanner/sizer/executor code."

Replica lag (Turso-specific reality, surfaced in the System Health tab):
  - primary_last_write_ms(): most-recent write the primary acknowledged.
    For local SQLite this is MAX(last_modified_ms) from the change_log.
    For libsql embedded-replica it's the server-side snapshot time.
  - replica_last_sync_ms(): when did we last pull from primary?
    For local SQLite this equals primary_last_write_ms (no lag).
    For libsql embedded-replica it's the client-side replication clock.
  - Lag = max(0, primary - replica).

  Dashboard reads the same StoreBackend, so its System Health tab shows
  whichever number this backend returns without caring which concrete
  class is in play.
"""

from __future__ import annotations

from typing import Any, Protocol


class StoreBackend(Protocol):
    """Minimum surface area every backend must implement. Intentionally
    narrow -- keep driver-specific quirks inside the concrete class."""

    # ---- lifecycle ----
    def connect(self) -> None: ...
    def close(self) -> None: ...

    # ---- sync API (write path via writer coroutine, read path for dashboard) ----
    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None: ...
    def executemany(self, sql: str, params: list[tuple[Any, ...]]) -> None: ...
    def executescript(self, sql: str) -> None: ...
    def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> tuple[Any, ...] | None: ...
    def fetch_many(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]: ...

    # ---- replica-lag telemetry (for System Health tab) ----
    def primary_last_write_ms(self) -> int | None: ...
    def replica_last_sync_ms(self) -> int | None: ...

    # ---- introspection (for logs / health checks) ----
    @property
    def driver_name(self) -> str: ...
