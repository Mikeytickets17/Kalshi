"""Typed configuration loaded from environment variables.

Single source of truth for all tunable parameters. Every module imports from
here rather than reading os.environ directly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _env(key: str, default: str | None = None, *, required: bool = False) -> str:
    val = os.environ.get(key, default)
    if required and not val:
        raise RuntimeError(f"Required env var {key} is missing")
    return val or ""


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    return int(raw)


def _env_float(key: str, default: float) -> float:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    return float(raw)


def _env_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key, "").lower()
    if raw == "":
        return default
    return raw in ("1", "true", "yes", "on")


def _env_list(key: str, default: list[str]) -> list[str]:
    raw = os.environ.get(key, "")
    if not raw:
        return default
    return [p.strip() for p in raw.split(",") if p.strip()]


@dataclass(frozen=True)
class Config:
    # Kalshi credentials
    kalshi_api_key_id: str
    kalshi_private_key_path: Path
    kalshi_use_demo: bool

    # Safety gates
    live_trading: bool
    kill_switch_file: Path
    daily_loss_limit_usd: float

    # Sizing
    hard_cap_usd: float
    min_edge_cents: float
    slippage_buffer_cents: float
    min_expected_profit_usd: float
    kelly_fraction: float

    # Universe
    universe_lookback_hours: int
    universe_min_volume_usd: float
    universe_categories: list[str]

    # Scanner
    scanner_min_halt_recover_sec: float
    scanner_empty_book_halt_sec: float

    # WS sharding
    ws_max_tickers_per_conn: int

    # Event store
    event_store_retention_days: int
    event_store_path: Path

    # Dashboard
    dashboard_user: str
    dashboard_password: str
    dashboard_port: int

    # Auto-publish
    auto_publish: bool
    auto_publish_branch: str

    @staticmethod
    def load() -> "Config":
        return Config(
            kalshi_api_key_id=_env("KALSHI_API_KEY_ID"),
            kalshi_private_key_path=Path(_env("KALSHI_PRIVATE_KEY_PATH", "./kalshi-private-key.pem")),
            kalshi_use_demo=_env_bool("KALSHI_USE_DEMO", True),
            live_trading=_env_bool("LIVE_TRADING", False),
            kill_switch_file=Path(_env("KILL_SWITCH_FILE", "./KILL_SWITCH")),
            daily_loss_limit_usd=_env_float("DAILY_LOSS_LIMIT_USD", 500.0),
            hard_cap_usd=_env_float("HARD_CAP_USD", 200.0),
            min_edge_cents=_env_float("MIN_EDGE_CENTS", 1.0),
            slippage_buffer_cents=_env_float("SLIPPAGE_BUFFER_CENTS", 0.5),
            min_expected_profit_usd=_env_float("MIN_EXPECTED_PROFIT_USD", 0.50),
            kelly_fraction=_env_float("KELLY_FRACTION", 0.5),
            universe_lookback_hours=_env_int("UNIVERSE_LOOKBACK_HOURS", 24),
            universe_min_volume_usd=_env_float("UNIVERSE_MIN_VOLUME_USD", 1000.0),
            universe_categories=_env_list("UNIVERSE_CATEGORIES", ["crypto", "weather", "econ"]),
            scanner_min_halt_recover_sec=_env_float("SCANNER_MIN_HALT_RECOVER_SEC", 5.0),
            scanner_empty_book_halt_sec=_env_float("SCANNER_EMPTY_BOOK_HALT_SEC", 2.0),
            ws_max_tickers_per_conn=_env_int("WS_MAX_TICKERS_PER_CONN", 100),
            event_store_retention_days=_env_int("EVENT_STORE_RETENTION_DAYS", 90),
            event_store_path=Path(_env("EVENT_STORE_PATH", "./data/kalshi.db")),
            dashboard_user=_env("DASHBOARD_USER", "admin"),
            dashboard_password=_env("DASHBOARD_PASSWORD", "changeme"),
            dashboard_port=_env_int("DASHBOARD_PORT", 5100),
            auto_publish=_env_bool("AUTO_PUBLISH", False),
            auto_publish_branch=_env("AUTO_PUBLISH_BRANCH", "kalshi-arb-data"),
        )


# Category → ticker prefix mapping. Used to filter the universe.
CATEGORY_PREFIXES: dict[str, tuple[str, ...]] = {
    "crypto": ("KXBTC", "KXETH"),
    "weather": ("KXHIGH", "KXLOW", "KXRAIN", "KXSNOW", "KXTEMP"),
    "econ": ("KXFED", "KXCPI", "KXNFP", "KXUNRATE", "KXPPI", "KXJOBS"),
}

# Economic-release series that halt around release windows.
# Scanner must suppress opportunities when these markets go paused or
# bilateral-empty.
HALT_PRONE_PREFIXES: tuple[str, ...] = (
    "KXFED", "KXCPI", "KXNFP", "KXUNRATE", "KXPPI", "KXJOBS",
)
