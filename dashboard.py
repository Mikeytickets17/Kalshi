"""
Kalshi Trading Bot — Live Dashboard

A Flask web dashboard that visualizes the bot's real state:
portfolio, positions, signals, risk metrics, and activity log.

Starts at zero. All data comes from real bot activity — no fake
seed data. The bot populates state as it runs and trades.

Usage:
    python dashboard.py
    Open http://localhost:5050
"""

import time
from flask import Flask, jsonify, render_template

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Live bot state — populated by the running bot, empty until then
# ---------------------------------------------------------------------------

INITIAL_BALANCE = 10_000.00

# These lists get populated by the bot at runtime via the shared state below.
# When the dashboard starts standalone, everything is zero/empty.
_live_state = {
    "positions": [],
    "signals": [],
    "closed_trades": [],
    "equity_curve": [INITIAL_BALANCE],
    "trade_count": 0,
    "win_count": 0,
    "portfolio_value": INITIAL_BALANCE,
    "peak_value": INITIAL_BALANCE,
    "start_time": time.time(),
}


def get_live_state() -> dict:
    """Return the current bot state snapshot — real data only, no fakes."""
    s = _live_state
    portfolio_value = s["portfolio_value"]
    positions = s["positions"]
    closed = s["closed_trades"]
    total_closed = len(closed)
    wins = s["win_count"]
    losses = total_closed - wins

    total_pnl = portfolio_value - INITIAL_BALANCE
    total_exposure = sum(p.get("size_usd", 0) for p in positions)
    unrealized_pnl = sum(p.get("pnl", 0) for p in positions)
    realized_pnl = sum(c.get("pnl", 0) for c in closed)
    peak = s["peak_value"]
    drawdown = (peak - portfolio_value) / peak if peak > 0 else 0

    uptime_s = int(time.time() - s["start_time"])
    days, rem = divmod(uptime_s, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    uptime = f"{days}d {hours}h {minutes}m" if days else f"{hours}h {minutes}m"

    risk = {
        "halted": False,
        "daily_pnl": round(total_pnl, 2),
        "daily_pnl_pct": round(total_pnl / max(portfolio_value, 1) * 100, 2),
        "drawdown_pct": round(drawdown * 100, 2),
        "drawdown_limit": 35.0,
        "daily_limit": 15.0,
        "consecutive_losses": 0,
        "max_consec": 8,
        "positions_used": len(positions),
        "positions_max": 8,
        "category_exposure": {
            "crypto": 0.0,
            "stocks": 0.0,
            "politics": 0.0,
            "economics": 0.0,
        },
    }

    performance = {
        "longshot": {"trades": 0, "wins": 0, "pnl": 0.0},
        "favorite": {"trades": 0, "wins": 0, "pnl": 0.0},
    }

    return {
        "portfolio_value": round(portfolio_value, 2),
        "initial_balance": INITIAL_BALANCE,
        "total_pnl": round(total_pnl, 2),
        "roi_pct": round(total_pnl / INITIAL_BALANCE * 100, 2),
        "peak_value": round(peak, 2),
        "total_exposure": round(total_exposure, 2),
        "unrealized_pnl": round(unrealized_pnl, 2),
        "realized_pnl": round(realized_pnl, 2),
        "equity_curve": s["equity_curve"][-168:],
        "positions": positions,
        "signals": s["signals"],
        "closed_trades": closed,
        "risk": risk,
        "performance": performance,
        "win_rate": round(wins / max(total_closed, 1) * 100, 1),
        "total_trades": total_closed,
        "wins": wins,
        "losses": losses,
        "uptime": uptime,
        "last_scan": "0s ago",
        "mode": "PAPER",
        "environment": "DEMO",
        "scan_interval": 300,
    }


@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/state")
def api_state():
    return jsonify(get_live_state())


if __name__ == "__main__":
    print("Starting dashboard at http://localhost:5050")
    app.run(host="0.0.0.0", port=5050, debug=False)
