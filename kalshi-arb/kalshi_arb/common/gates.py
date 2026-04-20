"""Startup gates that block unsafe production activity.

Hard rule: before any Module 2+ subsystem acts against prod, a fresh prod
probe must be on disk. Scanner config refuses to load production latency
/ rate-limit values until config/detected_limits.yaml has environment:prod
and a timestamp under PROD_PROBE_TTL_HOURS old.

Not a suggestion. The scanner imports require_prod_probe() and calls it
at startup when live_trading is enabled. Violation raises GateError
which prevents the process from entering the main loop.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


class GateError(RuntimeError):
    """Raised when a gate refuses to let the system proceed."""


PROD_PROBE_TTL_HOURS = 24.0
DEFAULT_PROBE_PATH = Path("config/detected_limits.yaml")


@dataclass(frozen=True)
class ProbeSnapshot:
    ts_utc: str
    environment: str
    age_hours: float
    payload: dict[str, Any]

    @property
    def is_prod(self) -> bool:
        return self.environment == "prod"

    @property
    def is_fresh(self) -> bool:
        return self.age_hours <= PROD_PROBE_TTL_HOURS


def load_probe(path: Path = DEFAULT_PROBE_PATH) -> ProbeSnapshot | None:
    """Load config/detected_limits.yaml or return None if missing/unreadable."""
    if not path.exists():
        return None
    try:
        with path.open() as f:
            data = yaml.safe_load(f) or {}
    except Exception:  # noqa: BLE001
        return None
    ts_raw = str(data.get("ts_utc") or "")
    env = str(data.get("environment") or "")
    age_hours = float("inf")
    if ts_raw:
        try:
            dt = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age_hours = (time.time() - dt.timestamp()) / 3600.0
        except (ValueError, TypeError):
            pass
    return ProbeSnapshot(ts_utc=ts_raw, environment=env, age_hours=age_hours, payload=data)


def require_prod_probe(
    *, live_trading: bool, path: Path = DEFAULT_PROBE_PATH
) -> ProbeSnapshot:
    """Block startup if live_trading=True without a fresh prod probe.

    - In demo/paper mode (live_trading=False), this function still loads
      whatever probe is available and returns it; it does NOT raise.
    - In live mode, it requires environment:prod + age < PROD_PROBE_TTL_HOURS.
    """
    snap = load_probe(path)
    if not live_trading:
        # Paper mode: just return whatever's on disk (may be None -> build a
        # placeholder so callers have a consistent return type).
        return snap or ProbeSnapshot(
            ts_utc="", environment="none", age_hours=float("inf"), payload={}
        )

    if snap is None:
        raise GateError(
            f"Live trading refused: {path} not found. Run the prod probe first."
        )
    if not snap.is_prod:
        raise GateError(
            f"Live trading refused: probe environment={snap.environment!r}, "
            f"must be 'prod'. Re-run the probe against production."
        )
    if not snap.is_fresh:
        raise GateError(
            f"Live trading refused: probe is {snap.age_hours:.1f}h old "
            f"(max {PROD_PROBE_TTL_HOURS}h). Re-run the prod probe."
        )
    return snap
