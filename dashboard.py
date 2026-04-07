"""
Kalshi Trading Bot — Live Dashboard

A Flask web dashboard that visualizes the bot's real state:
portfolio, positions, signals, risk metrics, and activity log.

Reads from shared_state (JSON file) written by the running bot.
Starts at zero. All data comes from real bot activity.

Usage:
    python dashboard.py
    Open http://localhost:5050
"""

import time
from flask import Flask, jsonify, render_template

import shared_state

app = Flask(__name__)

INITIAL_BALANCE = 10_000.00


def get_dashboard_state() -> dict:
    """Return state for the dashboard — from bot's shared state or empty."""
    # Try loading from shared state (bot writes this)
    disk_state = shared_state.load_from_disk()
    if disk_state and disk_state.get("bot_running"):
        return _format_bot_state(disk_state)

    # Also check in-memory state (if bot and dashboard are in same process)
    snap = shared_state.get_snapshot()
    if snap.get("bot_running"):
        return _format_bot_state(snap)

    # Bot not running — return empty state
    return _empty_state()


def _format_bot_state(s: dict) -> dict:
    """Format the raw bot state into dashboard-friendly JSON."""
    portfolio_value = s.get("portfolio_value", INITIAL_BALANCE)
    initial = s.get("initial_balance", INITIAL_BALANCE)
    peak = s.get("peak_value", INITIAL_BALANCE)
    closed = s.get("closed_trades", [])
    active = s.get("active_positions", [])
    total_closed = len(closed)
    wins = s.get("win_count", 0)
    losses = total_closed - wins
    total_pnl = portfolio_value - initial
    drawdown = (peak - portfolio_value) / peak if peak > 0 else 0

    uptime_s = int(time.time() - s.get("start_time", time.time()))
    days, rem = divmod(uptime_s, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    uptime = f"{days}d {hours}h {minutes}m" if days else f"{hours}h {minutes}m"

    last_updated = s.get("last_updated", time.time())
    scan_ago = int(time.time() - last_updated)
    scan_str = f"{scan_ago}s ago" if scan_ago < 60 else f"{scan_ago // 60}m ago"

    return {
        "bot_running": True,
        "portfolio_value": round(portfolio_value, 2),
        "initial_balance": initial,
        "total_pnl": round(total_pnl, 2),
        "roi_pct": round(total_pnl / initial * 100, 2) if initial else 0,
        "peak_value": round(peak, 2),
        "total_exposure": round(sum(p.get("size_usd", 0) for p in active), 2),
        "unrealized_pnl": round(sum(p.get("unrealized_pnl", 0) for p in active), 2),
        "realized_pnl": round(sum(c.get("pnl", 0) for c in closed), 2),
        "equity_curve": s.get("equity_curve", [initial])[-168:],
        "positions": active,
        "signals": s.get("signals", []),
        "closed_trades": closed,
        "trump_posts": s.get("trump_posts", []),
        "news_items": s.get("news_items", []),
        "risk": s.get("risk", {}),
        "win_rate": round(wins / max(total_closed, 1) * 100, 1),
        "total_trades": total_closed,
        "wins": wins,
        "losses": losses,
        "trade_count": s.get("trade_count", 0),
        "win_count": wins,
        "drawdown_pct": round(drawdown * 100, 2),
        "uptime": uptime,
        "last_scan": scan_str,
        "mode": "PAPER",
        "environment": "DEMO",
    }


def _empty_state() -> dict:
    """Empty dashboard state when bot is not running."""
    return {
        "bot_running": False,
        "portfolio_value": INITIAL_BALANCE,
        "initial_balance": INITIAL_BALANCE,
        "total_pnl": 0,
        "roi_pct": 0,
        "peak_value": INITIAL_BALANCE,
        "total_exposure": 0,
        "unrealized_pnl": 0,
        "realized_pnl": 0,
        "equity_curve": [INITIAL_BALANCE],
        "positions": [],
        "signals": [],
        "closed_trades": [],
        "trump_posts": [],
        "news_items": [],
        "risk": {},
        "win_rate": 0,
        "total_trades": 0,
        "wins": 0,
        "losses": 0,
        "trade_count": 0,
        "win_count": 0,
        "drawdown_pct": 0,
        "uptime": "0h 0m",
        "last_scan": "Not running",
        "mode": "PAPER",
        "environment": "DEMO",
    }


@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/state")
def api_state():
    return jsonify(get_dashboard_state())


if __name__ == "__main__":
    print("Starting dashboard at http://localhost:5050")
    app.run(host="0.0.0.0", port=5050, debug=False)
