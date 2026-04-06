"""
Kalshi Longshot Bias Bot — Live Dashboard

A Flask web dashboard that visualizes the bot's state:
portfolio, positions, signals, risk metrics, and activity log.

Runs with simulated data when the bot isn't active, so you can
see exactly what the system looks like in action.

Usage:
    python dashboard.py
    Open http://localhost:5050
"""

import math
import random
import time
from datetime import datetime, timedelta
from flask import Flask, jsonify, render_template

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Simulated state — mirrors what the live bot tracks internally
# ---------------------------------------------------------------------------

random.seed(int(time.time()))

INITIAL_BALANCE = 10_000.00
_sim_start = time.time() - 86400 * 7  # pretend bot has been running 7 days


def _generate_sim_data() -> dict:
    """Build a full snapshot of simulated bot state."""
    random.seed(42)  # deterministic for consistent demo

    # --- Portfolio equity curve (7 days, hourly) ---
    equity = [INITIAL_BALANCE]
    ts_points = []
    t = _sim_start
    for _ in range(7 * 24):
        change = random.gauss(1.8, 12)
        equity.append(round(equity[-1] + change, 2))
        ts_points.append(t)
        t += 3600
    portfolio_value = equity[-1]
    peak = max(equity)
    drawdown = (peak - portfolio_value) / peak

    # --- Active positions ---
    positions = [
        {
            "ticker": "SPORTS-NBA-UPSET-APR6",
            "title": "Will the 8-seed upset the 1-seed tonight?",
            "side": "NO",
            "type": "longshot",
            "category": "sports",
            "entry_price": 0.92,
            "current_price": 0.93,
            "size_usd": 285.00,
            "pnl": round(285 * (0.93 - 0.92), 2),
            "entry_time": "2h ago",
            "confidence": 0.72,
        },
        {
            "ticker": "ECON-FED-HOLD-APR",
            "title": "Will the Fed hold rates in April?",
            "side": "YES",
            "type": "favorite",
            "category": "economics",
            "entry_price": 0.78,
            "current_price": 0.80,
            "size_usd": 195.00,
            "pnl": round(195 * (0.80 - 0.78), 2),
            "entry_time": "5h ago",
            "confidence": 0.68,
        },
        {
            "ticker": "SPORTS-MLB-PIRATES-WIN",
            "title": "Will the Pirates win tonight?",
            "side": "NO",
            "type": "longshot",
            "category": "sports",
            "entry_price": 0.88,
            "current_price": 0.86,
            "size_usd": 150.00,
            "pnl": round(150 * (0.88 - 0.86) * -1, 2),
            "entry_time": "45m ago",
            "confidence": 0.61,
        },
    ]

    total_pnl = sum(p["pnl"] for p in positions)
    total_exposure = sum(p["size_usd"] for p in positions)

    # --- Recent signals ---
    signals = [
        {
            "time": "2m ago",
            "ticker": "ENT-OSCARS-ANIMATED-UPSET",
            "title": "Will an indie film win Best Animated Feature?",
            "side": "NO",
            "type": "longshot",
            "yes_price": 0.07,
            "edge": 0.028,
            "score": 0.71,
            "action": "TRADED",
            "status": "success",
        },
        {
            "time": "18m ago",
            "ticker": "SPORTS-NFL-DRAFT-QB1",
            "title": "Will a QB go #1 overall?",
            "side": "YES",
            "type": "favorite",
            "yes_price": 0.82,
            "edge": 0.025,
            "score": 0.63,
            "action": "TRADED",
            "status": "success",
        },
        {
            "time": "22m ago",
            "ticker": "SPORTS-GOLF-TIGER-WIN",
            "title": "Will Tiger Woods win the Masters?",
            "side": "NO",
            "type": "longshot",
            "yes_price": 0.04,
            "edge": 0.016,
            "score": 0.48,
            "action": "REJECTED",
            "status": "warning",
        },
        {
            "time": "35m ago",
            "ticker": "POL-APPROVAL-ABOVE60",
            "title": "Will presidential approval exceed 60%?",
            "side": "NO",
            "type": "longshot",
            "yes_price": 0.11,
            "edge": 0.044,
            "score": 0.77,
            "action": "TRADED",
            "status": "success",
        },
        {
            "time": "1h ago",
            "ticker": "ECON-CPI-UNDER-3",
            "title": "Will CPI come in under 3%?",
            "side": "YES",
            "type": "favorite",
            "yes_price": 0.74,
            "edge": 0.025,
            "score": 0.52,
            "action": "REJECTED",
            "status": "warning",
        },
        {
            "time": "1h ago",
            "ticker": "SPORTS-UFC-UNDERDOG",
            "title": "Will the +400 underdog win?",
            "side": "NO",
            "type": "longshot",
            "yes_price": 0.09,
            "edge": 0.036,
            "score": 0.69,
            "action": "TRADED",
            "status": "success",
        },
    ]

    # --- Closed trades (recent history) ---
    closed = [
        {"ticker": "SPORTS-NHL-UPSET-APR5", "side": "NO", "type": "longshot",
         "entry": 0.91, "exit": 0.94, "pnl": 8.40, "result": "win", "closed": "3h ago"},
        {"ticker": "ECON-JOBS-ABOVE-200K", "side": "YES", "type": "favorite",
         "entry": 0.76, "exit": 0.82, "pnl": 12.60, "result": "win", "closed": "6h ago"},
        {"ticker": "SPORTS-SOCCER-DRAW", "side": "NO", "type": "longshot",
         "entry": 0.87, "exit": 0.72, "pnl": -22.50, "result": "loss", "closed": "8h ago"},
        {"ticker": "ENT-GRAMMY-UPSET", "side": "NO", "type": "longshot",
         "entry": 0.93, "exit": 0.96, "pnl": 5.10, "result": "win", "closed": "12h ago"},
        {"ticker": "POL-SENATE-VOTE-YES", "side": "YES", "type": "favorite",
         "entry": 0.80, "exit": 0.85, "pnl": 10.00, "result": "win", "closed": "14h ago"},
        {"ticker": "SPORTS-F1-VERSTAPPEN", "side": "YES", "type": "favorite",
         "entry": 0.72, "exit": 0.68, "pnl": -8.00, "result": "loss", "closed": "18h ago"},
        {"ticker": "SPORTS-MLB-DODGERS", "side": "NO", "type": "longshot",
         "entry": 0.90, "exit": 0.93, "pnl": 6.30, "result": "win", "closed": "20h ago"},
        {"ticker": "ECON-RETAIL-UP", "side": "YES", "type": "favorite",
         "entry": 0.77, "exit": 0.81, "pnl": 7.20, "result": "win", "closed": "1d ago"},
    ]

    total_closed = len(closed)
    wins = sum(1 for c in closed if c["result"] == "win")
    losses = total_closed - wins
    total_realized = sum(c["pnl"] for c in closed)

    # --- Risk state ---
    risk = {
        "halted": False,
        "daily_pnl": round(total_pnl + 14.20, 2),
        "daily_pnl_pct": round((total_pnl + 14.20) / portfolio_value * 100, 2),
        "drawdown_pct": round(drawdown * 100, 2),
        "drawdown_limit": 35.0,
        "daily_limit": 15.0,
        "consecutive_losses": 1,
        "max_consec": 8,
        "positions_used": len(positions),
        "positions_max": 8,
        "category_exposure": {
            "sports": round(sum(p["size_usd"] for p in positions if p["category"] == "sports") / portfolio_value * 100, 1),
            "economics": round(sum(p["size_usd"] for p in positions if p["category"] == "economics") / portfolio_value * 100, 1),
            "politics": 0.0,
            "entertainment": 0.0,
        },
    }

    # --- Performance by type ---
    performance = {
        "longshot": {"trades": 18, "wins": 14, "pnl": 82.40},
        "favorite": {"trades": 12, "wins": 9, "pnl": 45.80},
    }

    return {
        "portfolio_value": round(portfolio_value, 2),
        "initial_balance": INITIAL_BALANCE,
        "total_pnl": round(portfolio_value - INITIAL_BALANCE, 2),
        "roi_pct": round((portfolio_value - INITIAL_BALANCE) / INITIAL_BALANCE * 100, 2),
        "peak_value": round(peak, 2),
        "total_exposure": round(total_exposure, 2),
        "unrealized_pnl": round(total_pnl, 2),
        "realized_pnl": round(total_realized, 2),
        "equity_curve": equity[-168:],  # last 7 days hourly
        "positions": positions,
        "signals": signals,
        "closed_trades": closed,
        "risk": risk,
        "performance": performance,
        "win_rate": round(wins / max(total_closed, 1) * 100, 1),
        "total_trades": total_closed,
        "wins": wins,
        "losses": losses,
        "uptime": "7d 4h 22m",
        "last_scan": "32s ago",
        "mode": "PAPER",
        "environment": "DEMO",
        "scan_interval": 300,
    }


@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/state")
def api_state():
    return jsonify(_generate_sim_data())


if __name__ == "__main__":
    print("Starting dashboard at http://localhost:5050")
    app.run(host="0.0.0.0", port=5050, debug=False)
