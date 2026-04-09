"""
Shared state between the bot and dashboard.

The bot writes its state here on every trade event, and the
dashboard reads it.  State is persisted to a JSON file so the
dashboard can pick it up even if it starts after the bot.

Thread-safe: uses a lock for in-process access and atomic file
writes for cross-process access.
"""

import json
import logging
import os
import tempfile
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

STATE_FILE = os.path.join(os.path.dirname(__file__), "bot_state.json")
_lock = threading.Lock()

# In-memory state — the canonical source while the bot is running
_state: dict[str, Any] = {
    "portfolio_value": 10000.0,
    "initial_balance": 10000.0,
    "peak_value": 10000.0,
    "trade_count": 0,
    "win_count": 0,
    "active_positions": [],
    "closed_trades": [],
    "signals": [],
    "equity_curve": [10000.0],
    "trump_posts": [],
    "news_items": [],
    "risk": {},
    "whale_signals": [],
    "whale_copies": [],
    "start_time": time.time(),
    "last_updated": time.time(),
    "bot_running": False,
}


def init(initial_balance: float = 10000.0) -> None:
    """Initialize state at bot startup."""
    with _lock:
        _state["portfolio_value"] = initial_balance
        _state["initial_balance"] = 10000.0  # Always use true starting capital for P&L
        _state["peak_value"] = initial_balance
        _state["equity_curve"] = [initial_balance]
        _state["trade_count"] = 0
        _state["win_count"] = 0
        _state["active_positions"] = []
        _state["closed_trades"] = []
        _state["signals"] = []
        _state["trump_posts"] = []
        _state["news_items"] = []
        _state["risk"] = {}
        _state["start_time"] = time.time()
        _state["last_updated"] = time.time()
        _state["bot_running"] = True
    _persist()
    logger.info("Shared state initialized (balance=$%.2f)", initial_balance)


def get_snapshot() -> dict:
    """Return a copy of the current state for the dashboard."""
    with _lock:
        return json.loads(json.dumps(_state))


# ── Trade lifecycle events ──

def record_trade_opened(
    trade_id: str,
    strategy: str,
    side: str,
    asset: str,
    venue: str,
    entry_price: float,
    size_usd: float,
    confidence: float = 0.0,
    reason: str = "",
) -> None:
    """Called when a new position is opened."""
    pos = {
        "id": trade_id,
        "strategy": strategy,
        "side": side,
        "asset": asset,
        "venue": venue,
        "entry_price": entry_price,
        "size_usd": size_usd,
        "confidence": confidence,
        "reason": reason,
        "opened_at": time.time(),
        "unrealized_pnl": 0.0,
    }
    with _lock:
        _state["active_positions"].append(pos)
        _state["last_updated"] = time.time()
    _persist()
    logger.debug("State: opened %s %s %s $%.2f", strategy, side, asset, size_usd)


def record_trade_closed(
    trade_id: str,
    pnl: float,
    exit_price: float = 0.0,
    reason: str = "",
) -> None:
    """Called when a position is fully closed."""
    with _lock:
        # Find and remove from active
        pos = None
        for i, p in enumerate(_state["active_positions"]):
            if p["id"] == trade_id:
                pos = _state["active_positions"].pop(i)
                break

        if not pos:
            logger.warning("State: tried to close unknown trade %s", trade_id)
            return

        won = pnl > 0
        _state["trade_count"] += 1
        if won:
            _state["win_count"] += 1

        _state["portfolio_value"] += pnl
        if _state["portfolio_value"] > _state["peak_value"]:
            _state["peak_value"] = _state["portfolio_value"]
        _state["equity_curve"].append(round(_state["portfolio_value"], 2))

        # Keep last 500 equity points
        if len(_state["equity_curve"]) > 500:
            _state["equity_curve"] = _state["equity_curve"][-500:]

        closed = {
            "id": trade_id,
            "strategy": pos["strategy"],
            "side": pos["side"],
            "asset": pos["asset"],
            "venue": pos["venue"],
            "entry_price": pos["entry_price"],
            "exit_price": exit_price,
            "size_usd": pos["size_usd"],
            "pnl": round(pnl, 2),
            "result": "WIN" if won else "LOSS",
            "opened_at": pos["opened_at"],
            "closed_at": time.time(),
            "reason": reason,
        }
        _state["closed_trades"].insert(0, closed)

        # Keep last 200 closed trades
        if len(_state["closed_trades"]) > 200:
            _state["closed_trades"] = _state["closed_trades"][:200]

        _state["last_updated"] = time.time()

    _persist()
    logger.debug(
        "State: closed %s %s pnl=$%.2f (%s)",
        pos["strategy"], pos["asset"], pnl, "WIN" if won else "LOSS",
    )


def record_signal(
    strategy: str,
    side: str,
    asset: str,
    venue: str,
    confidence: float,
    reason: str,
    action: str = "TRADED",
) -> None:
    """Record a signal the bot evaluated (traded or rejected)."""
    sig = {
        "time": time.time(),
        "strategy": strategy,
        "side": side,
        "asset": asset,
        "venue": venue,
        "confidence": confidence,
        "reason": reason,
        "action": action,
    }
    with _lock:
        _state["signals"].insert(0, sig)
        if len(_state["signals"]) > 50:
            _state["signals"] = _state["signals"][:50]
        _state["last_updated"] = time.time()
    # Don't persist every signal — too frequent
    # The next trade event or periodic flush will persist


def record_trump_post(text: str, source: str, sentiment: str = "", confidence: float = 0.0) -> None:
    """Record a detected Trump post."""
    post = {
        "text": text,
        "source": source,
        "detected_at": time.time(),
        "sentiment": sentiment,
        "confidence": confidence,
    }
    with _lock:
        _state["trump_posts"].insert(0, post)
        if len(_state["trump_posts"]) > 20:
            _state["trump_posts"] = _state["trump_posts"][:20]
        _state["last_updated"] = time.time()
    _persist()


def record_news(headline: str, source: str, priority: str, category: str = "") -> None:
    """Record a detected news item."""
    item = {
        "headline": headline,
        "source": source,
        "priority": priority,
        "category": category,
        "detected_at": time.time(),
    }
    with _lock:
        _state["news_items"].insert(0, item)
        if len(_state["news_items"]) > 30:
            _state["news_items"] = _state["news_items"][:30]
        _state["last_updated"] = time.time()
    _persist()


def record_whale_signal(signal_data: dict) -> None:
    """Record a detected whale activity signal."""
    with _lock:
        _state["whale_signals"].insert(0, signal_data)
        if len(_state["whale_signals"]) > 50:
            _state["whale_signals"] = _state["whale_signals"][:50]
        _state["last_updated"] = time.time()


def record_whale_copy(copy_data: dict) -> None:
    """Record a whale copy trade."""
    with _lock:
        _state["whale_copies"].insert(0, copy_data)
        if len(_state["whale_copies"]) > 30:
            _state["whale_copies"] = _state["whale_copies"][:30]
        _state["last_updated"] = time.time()
    _persist()


def update_position_pnl(trade_id: str, unrealized_pnl: float) -> None:
    """Update unrealized P&L for an active position."""
    with _lock:
        for pos in _state["active_positions"]:
            if pos["id"] == trade_id:
                pos["unrealized_pnl"] = round(unrealized_pnl, 2)
                break


def update_portfolio(value: float) -> None:
    """Update portfolio value (e.g. from unrealized P&L changes)."""
    with _lock:
        _state["portfolio_value"] = round(value, 2)
        if value > _state["peak_value"]:
            _state["peak_value"] = round(value, 2)
        _state["last_updated"] = time.time()


def update_risk(risk_data: dict) -> None:
    """Update risk management state."""
    with _lock:
        _state["risk"] = risk_data
        _state["last_updated"] = time.time()


def set_bot_running(running: bool) -> None:
    """Mark whether the bot is currently running."""
    with _lock:
        _state["bot_running"] = running
        _state["last_updated"] = time.time()
    _persist()


# ── Persistence ──

def _persist() -> None:
    """Atomically write state to disk."""
    try:
        data = get_snapshot()
        # Write to temp file then rename (atomic on POSIX)
        dir_name = os.path.dirname(STATE_FILE)
        fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
            os.replace(tmp_path, STATE_FILE)
        except Exception:
            os.unlink(tmp_path)
            raise
    except Exception as exc:
        logger.debug("Failed to persist state: %s", exc)


def load_from_disk() -> dict | None:
    """Load state from disk (for dashboard startup)."""
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
            # Check if state is recent (< 24 hours)
            if time.time() - data.get("last_updated", 0) < 86400:
                return data
    except Exception as exc:
        logger.debug("Failed to load state from disk: %s", exc)
    return None


def periodic_flush() -> None:
    """Call this periodically (e.g. every 5s) to ensure state is on disk."""
    _persist()
