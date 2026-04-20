"""Dashboard env-driven config. Single source of truth."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from .._paths import default_event_store_path as _default_event_store_path


def _env(key: str, default: str | None = None) -> str:
    return os.environ.get(key, default) or ""


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    return int(raw)


@dataclass(frozen=True)
class DashboardConfig:
    # Auth
    username: str
    password: str                 # cleartext comparison via secrets.compare_digest
    session_secret: str           # for signed cookies; rotated means logout-all

    # Server
    port: int

    # Rate limiting
    login_rate_per_min_per_ip: int

    # Data source: path to the SQLite event store the bot is writing to.
    # Paper phase: both processes run on the same laptop, point here.
    # Live phase: swapped for Turso via LIBSQL_* (below).
    event_store_path: Path

    # If True, on dashboard startup the change-capture task replays ALL
    # existing change_log rows to newly-connected clients. Default False
    # (live-from-now-forward). Flip True for local debugging only.
    replay_backlog_on_start: bool

    # Live-migration fields (unused in paper phase; see docs/turso-setup.md)
    libsql_url: str
    libsql_auth_token: str
    libsql_sync_url: str
    libsql_local_path: Path

    @staticmethod
    def load() -> "DashboardConfig":
        password = _env("DASHBOARD_PASSWORD")
        if not password:
            # A missing password at runtime is a hard error. Fail closed --
            # the dashboard refuses to start rather than allowing an empty
            # or default password to become live.
            raise RuntimeError(
                "DASHBOARD_PASSWORD is not set. The dashboard refuses to "
                "start without an explicit password. Set it via the "
                "launcher (auto-generated into .dashboard_creds on first "
                "run) or export it for local testing."
            )
        session_secret = _env("DASHBOARD_SESSION_SECRET")
        if not session_secret:
            # Generate one at startup. This is fine for single-instance
            # deployments -- process restart invalidates all sessions and
            # the operator logs in again.
            session_secret = secrets.token_urlsafe(48)
        return DashboardConfig(
            username=_env("DASHBOARD_USERNAME", "admin"),
            password=password,
            session_secret=session_secret,
            port=_env_int("PORT", 8080),
            login_rate_per_min_per_ip=_env_int("DASHBOARD_LOGIN_RATE", 60),
            # Absolute path resolved from package location so dashboard
            # and bot can never disagree about which file holds the data.
            # Override via EVENT_STORE_PATH for testing / live deployment.
            event_store_path=_default_event_store_path(),
            replay_backlog_on_start=(_env("DASHBOARD_REPLAY_BACKLOG", "").lower() in ("1", "true", "yes")),
            libsql_url=_env("LIBSQL_URL"),
            libsql_auth_token=_env("LIBSQL_AUTH_TOKEN"),
            libsql_sync_url=_env("LIBSQL_SYNC_URL"),
            libsql_local_path=Path(_env("LIBSQL_LOCAL_PATH", "/data/replica.db")),
        )
