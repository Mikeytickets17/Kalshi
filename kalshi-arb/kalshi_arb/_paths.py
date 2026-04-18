"""Shared path helpers.

The dashboard, the verifier, the simulate_events tool, and the bot
itself MUST all agree on where the SQLite event store lives -- otherwise
the bot writes to one file and the dashboard reads from another and you
get 'where did my data go' bugs.

Resolving from __file__ (this module) makes the path deterministic
regardless of which process's CWD happens to be at the time. EVENT_STORE_PATH
env var still wins if set, so live deployment can override.
"""

from __future__ import annotations

import os
from pathlib import Path


def repo_root() -> Path:
    """The kalshi-arb/ directory (where pyproject.toml lives).

    __file__               kalshi-arb/kalshi_arb/_paths.py
    parents[0]             kalshi-arb/kalshi_arb
    parents[1]             kalshi-arb
    """
    return Path(__file__).resolve().parents[1]


def default_event_store_path() -> Path:
    """The single canonical location for the SQLite event store.

    Override with EVENT_STORE_PATH env var if you need a different file
    (live deployment, multi-bot testing, etc.). Otherwise everyone
    -- bot, dashboard, verifier, simulate -- uses this absolute path.
    """
    override = os.environ.get("EVENT_STORE_PATH")
    if override:
        return Path(override).expanduser().resolve()
    return repo_root() / "data" / "kalshi.db"
