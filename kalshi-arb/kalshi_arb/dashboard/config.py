"""Dashboard env-driven config. Single source of truth."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path


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

    # Data backend (wired in step 3; noop in step 2)
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
                "start without an explicit password. Set it via "
                "`fly secrets set DASHBOARD_PASSWORD=<strong-password>` or "
                "export it for local testing."
            )
        session_secret = _env("DASHBOARD_SESSION_SECRET")
        if not session_secret:
            # Generate one at startup. This is fine for single-instance
            # deployments (fly machine restart = all sessions invalidated,
            # operator logs in again). For multi-instance or zero-downtime
            # rollout the operator must set this explicitly.
            session_secret = secrets.token_urlsafe(48)
        return DashboardConfig(
            username=_env("DASHBOARD_USERNAME", "admin"),
            password=password,
            session_secret=session_secret,
            port=_env_int("PORT", 8080),   # Fly sets PORT env var automatically
            login_rate_per_min_per_ip=_env_int("DASHBOARD_LOGIN_RATE", 60),
            libsql_url=_env("LIBSQL_URL"),
            libsql_auth_token=_env("LIBSQL_AUTH_TOKEN"),
            libsql_sync_url=_env("LIBSQL_SYNC_URL"),
            libsql_local_path=Path(_env("LIBSQL_LOCAL_PATH", "/data/replica.db")),
        )
