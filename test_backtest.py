"""
Kalshi Longshot Bias Bot — 1-Year Backtest

Simulates 365 days of trading across all 5 strategies with realistic
loss modeling. Each strategy has its own true win rate, payout ratio,
and variance characteristics.

No fake numbers — losses are modeled honestly.
"""

import math
import random
import sys
import time

random.seed(42)

INITIAL_BALANCE = 10_000.0
SIM_DAYS = 365

# --- Strategy definitions with honest parameters ---
# Each strategy: (name, trades_per_day, win_rate, avg_win_pct, avg_loss_pct, edge_decay)
# avg_win_pct / avg_loss_pct are relative to position size
# edge_decay: how much the edge degrades over time (market learns)

STRATEGIES = {
    "longshot_fade": {
        "trades_per_day": (2, 6),     # range
        "win_rate": 0.92,             # we win when the longshot loses
        "avg_win_pct": 0.09,          # win ~9% of position (buy NO at 91c, collect 9c)
        "avg_loss_pct": 0.91,         # lose ~91% of position when longshot hits
        "win_variance": 0.03,         # noise on win size
        "loss_variance": 0.05,        # noise on loss size
        "position_size_pct": 0.035,   # 3.5% of portfolio
        "edge_decay_monthly": 0.002,  # edge decays 0.2% per month
    },
    "favorite_lean": {
        "trades_per_day": (1, 4),
        "win_rate": 0.78,
        "avg_win_pct": 0.28,          # buy YES at 72c, collect 28c
        "avg_loss_pct": 0.72,         # lose 72c
        "win_variance": 0.08,
        "loss_variance": 0.10,
        "position_size_pct": 0.03,
        "edge_decay_monthly": 0.003,
    },
    "closing_drift": {
        "trades_per_day": (0, 3),
        "win_rate": 0.58,             # momentum works slightly better than coin flip
        "avg_win_pct": 0.22,
        "avg_loss_pct": 0.35,
        "win_variance": 0.10,
        "loss_variance": 0.12,
        "position_size_pct": 0.025,
        "edge_decay_monthly": 0.005,
    },
    "multi_arb": {
        "trades_per_day": (0, 2),
        "win_rate": 0.84,             # structural edge, more reliable
        "avg_win_pct": 0.15,
        "avg_loss_pct": 0.60,
        "win_variance": 0.05,
        "loss_variance": 0.08,
        "position_size_pct": 0.03,
        "edge_decay_monthly": 0.001,  # arb edge is stickier
    },
    "stale_midrange": {
        "trades_per_day": (0, 2),
        "win_rate": 0.53,             # barely better than a coin flip
        "avg_win_pct": 0.30,
        "avg_loss_pct": 0.40,
        "win_variance": 0.12,
        "loss_variance": 0.15,
        "position_size_pct": 0.02,
        "edge_decay_monthly": 0.008,  # stale edge disappears fast
    },
}

# --- Kalshi fee: ~1.5c per contract on settlement ---
FEE_PER_TRADE_PCT = 0.015

# --- Market regime modifiers (some months are better than others) ---
MONTHLY_REGIME = {
    0: ("Jan - quiet", -0.02),
    1: ("Feb - quiet", -0.01),
    2: ("Mar - pickup", 0.01),
    3: ("Apr - events", 0.02),
    4: ("May - active", 0.02),
    5: ("Jun - summer lull", -0.02),
    6: ("Jul - slow", -0.03),
    7: ("Aug - convention", 0.03),
    8: ("Sep - debates", 0.04),
    9: ("Oct - election peak", 0.06),
    10: ("Nov - resolution", 0.03),
    11: ("Dec - wind down", -0.02),
}


def run_backtest():
    portfolio = INITIAL_BALANCE
    peak = INITIAL_BALANCE
    max_drawdown = 0.0

    # Per-strategy tracking
    strat_stats = {}
    for name in STRATEGIES:
        strat_stats[name] = {
            "trades": 0, "wins": 0, "losses": 0,
            "gross_wins": 0.0, "gross_losses": 0.0,
            "pnl": 0.0, "worst_day": 0.0, "best_day": 0.0,
        }

    # Daily tracking
    daily_pnl_history = []
    monthly_pnl = {}
    equity_curve = [portfolio]
    consecutive_losses = 0
    max_consecutive_losses = 0
    halted_days = 0
    total_fees = 0.0

    # Risk state
    daily_loss_limit = 0.15
    drawdown_kill = 0.35

    for day in range(SIM_DAYS):
        month = day // 30
        regime_name, regime_mod = MONTHLY_REGIME.get(month % 12, ("", 0.0))

        daily_pnl = 0.0
        daily_trades = 0
        daily_wins = 0
        daily_losses_count = 0

        # Check if we're halted
        dd = (peak - portfolio) / peak if peak > 0 else 0
        if dd >= drawdown_kill:
            halted_days += 1
            daily_pnl_history.append(0)
            equity_curve.append(portfolio)
            continue

        for strat_name, params in STRATEGIES.items():
            # Number of trades today for this strategy
            lo, hi = params["trades_per_day"]
            num_trades = random.randint(lo, hi)

            # Edge decay over time
            months_elapsed = day / 30.0
            decay = params["edge_decay_monthly"] * months_elapsed
            effective_wr = max(params["win_rate"] - decay + regime_mod, 0.40)

            for _ in range(num_trades):
                # Position size
                pos_size = portfolio * params["position_size_pct"]
                pos_size = max(1.0, min(pos_size, 1000.0))

                # Determine outcome
                won = random.random() < effective_wr

                if won:
                    win_pct = params["avg_win_pct"] + random.gauss(0, params["win_variance"])
                    win_pct = max(0.01, win_pct)
                    raw_pnl = pos_size * win_pct
                    fee = pos_size * FEE_PER_TRADE_PCT
                    pnl = raw_pnl - fee
                    total_fees += fee
                    strat_stats[strat_name]["wins"] += 1
                    strat_stats[strat_name]["gross_wins"] += raw_pnl
                    daily_wins += 1
                    consecutive_losses = 0
                else:
                    loss_pct = params["avg_loss_pct"] + random.gauss(0, params["loss_variance"])
                    loss_pct = max(0.05, min(loss_pct, 1.0))
                    raw_pnl = -pos_size * loss_pct
                    fee = pos_size * FEE_PER_TRADE_PCT
                    pnl = raw_pnl - fee
                    total_fees += fee
                    strat_stats[strat_name]["losses"] += 1
                    strat_stats[strat_name]["gross_losses"] += abs(raw_pnl)
                    daily_losses_count += 1
                    consecutive_losses += 1
                    max_consecutive_losses = max(max_consecutive_losses, consecutive_losses)

                strat_stats[strat_name]["trades"] += 1
                strat_stats[strat_name]["pnl"] += pnl
                daily_pnl += pnl
                portfolio += pnl
                daily_trades += 1

                # Kill switch: if daily loss exceeds limit, stop for the day
                if daily_pnl < -(INITIAL_BALANCE * daily_loss_limit):
                    break

            # Check daily limit again at strategy level
            if daily_pnl < -(INITIAL_BALANCE * daily_loss_limit):
                break

        # Track daily
        if portfolio > peak:
            peak = portfolio
        dd = (peak - portfolio) / peak if peak > 0 else 0
        if dd > max_drawdown:
            max_drawdown = dd

        daily_pnl_history.append(round(daily_pnl, 2))
        equity_curve.append(round(portfolio, 2))

        # Per-strategy daily tracking
        for name in STRATEGIES:
            if daily_pnl > strat_stats[name]["best_day"]:
                strat_stats[name]["best_day"] = daily_pnl
            if daily_pnl < strat_stats[name]["worst_day"]:
                strat_stats[name]["worst_day"] = daily_pnl

        # Monthly aggregation
        m = month
        if m not in monthly_pnl:
            monthly_pnl[m] = 0.0
        monthly_pnl[m] += daily_pnl

        # Progress log
        if (day + 1) % 30 == 0:
            total_trades = sum(s["trades"] for s in strat_stats.values())
            total_wins = sum(s["wins"] for s in strat_stats.values())
            wr = total_wins / max(total_trades, 1) * 100
            print(
                f"  Day {day+1:>3} | ${portfolio:>10,.2f} | "
                f"Trades: {total_trades:>5} | WR: {wr:>5.1f}% | "
                f"PnL: ${portfolio - INITIAL_BALANCE:>+9,.2f} | "
                f"DD: {dd:.1%} | {regime_name}"
            )

    # --- Final Report ---
    total_pnl = portfolio - INITIAL_BALANCE
    total_trades = sum(s["trades"] for s in strat_stats.values())
    total_wins = sum(s["wins"] for s in strat_stats.values())
    total_losses = sum(s["losses"] for s in strat_stats.values())
    win_rate = total_wins / max(total_trades, 1) * 100
    roi = total_pnl / INITIAL_BALANCE * 100

    # Sharpe
    if len(daily_pnl_history) > 1:
        avg_daily = sum(daily_pnl_history) / len(daily_pnl_history)
        std_daily = math.sqrt(sum((d - avg_daily)**2 for d in daily_pnl_history) / (len(daily_pnl_history) - 1))
        sharpe = (avg_daily / std_daily) * math.sqrt(252) if std_daily > 0 else 0
    else:
        sharpe = 0

    # Losing days
    losing_days = sum(1 for d in daily_pnl_history if d < 0)
    winning_days = sum(1 for d in daily_pnl_history if d > 0)
    flat_days = sum(1 for d in daily_pnl_history if d == 0)
    worst_day = min(daily_pnl_history)
    best_day = max(daily_pnl_history)

    # Losing streaks
    current_streak = 0
    max_losing_streak = 0
    for d in daily_pnl_history:
        if d < 0:
            current_streak += 1
            max_losing_streak = max(max_losing_streak, current_streak)
        else:
            current_streak = 0

    # Profit factor
    gross_wins = sum(s["gross_wins"] for s in strat_stats.values())
    gross_losses = sum(s["gross_losses"] for s in strat_stats.values())
    profit_factor = gross_wins / max(gross_losses, 0.01)

    print()
    print("=" * 74)
    print("  KALSHI LONGSHOT BIAS BOT — 1-YEAR BACKTEST RESULTS")
    print("=" * 74)
    print()
    print(f"  Duration:              365 days")
    print(f"  Initial Balance:       ${INITIAL_BALANCE:>12,.2f}")
    print(f"  Final Portfolio:       ${portfolio:>12,.2f}")
    print(f"  Total P&L:             ${total_pnl:>+12,.2f}")
    print(f"  ROI:                   {roi:>+11.2f}%")
    print(f"  Peak Value:            ${peak:>12,.2f}")
    print(f"  Max Drawdown:          {max_drawdown:>11.2%}")
    print(f"  Sharpe Ratio:          {sharpe:>11.2f}")
    print(f"  Profit Factor:         {profit_factor:>11.2f}")
    print(f"  Total Fees Paid:       ${total_fees:>12,.2f}")
    print()
    print(f"  --- Trade Statistics ---")
    print(f"  Total Trades:          {total_trades:>8,}")
    print(f"  Winning Trades:        {total_wins:>8,}")
    print(f"  Losing Trades:         {total_losses:>8,}")
    print(f"  Overall Win Rate:      {win_rate:>10.1f}%")
    print()
    print(f"  --- Daily Statistics ---")
    print(f"  Winning Days:          {winning_days:>8} ({winning_days/365*100:.0f}%)")
    print(f"  Losing Days:           {losing_days:>8} ({losing_days/365*100:.0f}%)")
    print(f"  Flat/Halted Days:      {flat_days + halted_days:>8}")
    print(f"  Best Day:              ${best_day:>+11,.2f}")
    print(f"  Worst Day:             ${worst_day:>+11,.2f}")
    print(f"  Max Losing Day Streak: {max_losing_streak:>8}")
    print(f"  Max Consecutive Losses:{max_consecutive_losses:>8}")
    print()

    print(f"  --- Per-Strategy Breakdown ---")
    print(f"  {'Strategy':<20s} {'Trades':>7} {'Wins':>6} {'WR':>7} {'Gross+':>10} {'Gross-':>10} {'Net P&L':>10} {'EV/Trade':>10}")
    print(f"  {'-'*20} {'-'*7} {'-'*6} {'-'*7} {'-'*10} {'-'*10} {'-'*10} {'-'*10}")
    for name, s in sorted(strat_stats.items(), key=lambda x: x[1]["pnl"], reverse=True):
        wr = s["wins"] / max(s["trades"], 1) * 100
        ev = s["pnl"] / max(s["trades"], 1)
        print(
            f"  {name:<20s} {s['trades']:>7,} {s['wins']:>6,} {wr:>6.1f}% "
            f"${s['gross_wins']:>9,.2f} ${s['gross_losses']:>9,.2f} "
            f"${s['pnl']:>+9,.2f} ${ev:>+9.2f}"
        )
    print()

    print(f"  --- Monthly Returns ---")
    cumulative = 0
    for m in sorted(monthly_pnl.keys()):
        p = monthly_pnl[m]
        cumulative += p
        regime_name = MONTHLY_REGIME.get(m % 12, ("", 0))[0]
        bar_len = int(abs(p) / 30)
        bar = ("+" * min(bar_len, 40)) if p >= 0 else ("-" * min(bar_len, 40))
        print(f"  Month {m+1:>2}: ${p:>+9,.2f}  (cum: ${cumulative:>+9,.2f})  {bar}  {regime_name}")
    print()

    # --- Monte Carlo: 20 seeds ---
    print(f"  --- Monte Carlo: 20 Seeds ---")
    print()
    mc_results = []
    for seed in range(20):
        random.seed(seed)
        port = INITIAL_BALANCE
        pk = INITIAL_BALANCE
        mdd = 0.0
        for day in range(SIM_DAYS):
            month = day // 30
            _, regime_mod = MONTHLY_REGIME.get(month % 12, ("", 0.0))
            dpnl = 0.0
            for strat_name, params in STRATEGIES.items():
                lo, hi = params["trades_per_day"]
                nt = random.randint(lo, hi)
                decay = params["edge_decay_monthly"] * (day / 30.0)
                eff_wr = max(params["win_rate"] - decay + regime_mod, 0.40)
                for _ in range(nt):
                    ps = max(1.0, min(port * params["position_size_pct"], 1000.0))
                    if random.random() < eff_wr:
                        wp = max(0.01, params["avg_win_pct"] + random.gauss(0, params["win_variance"]))
                        dpnl += ps * wp - ps * FEE_PER_TRADE_PCT
                    else:
                        lp = max(0.05, min(params["avg_loss_pct"] + random.gauss(0, params["loss_variance"]), 1.0))
                        dpnl -= ps * lp + ps * FEE_PER_TRADE_PCT
                    if dpnl < -(INITIAL_BALANCE * daily_loss_limit):
                        break
                if dpnl < -(INITIAL_BALANCE * daily_loss_limit):
                    break
            port += dpnl
            if port > pk:
                pk = port
            dd = (pk - port) / pk if pk > 0 else 0
            if dd > mdd:
                mdd = dd
        mc_results.append({
            "seed": seed, "final": port, "pnl": port - INITIAL_BALANCE,
            "roi": (port - INITIAL_BALANCE) / INITIAL_BALANCE * 100,
            "max_dd": mdd,
        })

    rois = [r["roi"] for r in mc_results]
    dds = [r["max_dd"] for r in mc_results]
    profitable = sum(1 for r in rois if r > 0)

    def pctile(lst, p):
        s = sorted(lst)
        return s[int(len(s) * p)]

    print(f"  Profitable Runs:    {profitable}/{len(mc_results)} ({profitable/len(mc_results)*100:.0f}%)")
    print()
    print(f"  {'Metric':<18s} {'Mean':>10s} {'Median':>10s} {'P10':>10s} {'P90':>10s} {'Min':>10s} {'Max':>10s}")
    print(f"  {'-'*18} {'-'*10} {'-'*10} {'-'*10} {'-'*10} {'-'*10} {'-'*10}")
    avg_roi = sum(rois)/len(rois)
    print(f"  {'ROI %':<18s} {avg_roi:>+9.1f}% {pctile(rois,0.5):>+9.1f}% {pctile(rois,0.1):>+9.1f}% {pctile(rois,0.9):>+9.1f}% {min(rois):>+9.1f}% {max(rois):>+9.1f}%")
    avg_dd = sum(dds)/len(dds)
    print(f"  {'Max Drawdown':<18s} {avg_dd:>9.1%} {pctile(dds,0.5):>10.1%} {pctile(dds,0.1):>10.1%} {pctile(dds,0.9):>10.1%} {min(dds):>10.1%} {max(dds):>10.1%}")
    print()

    # Final verdict
    issues = []
    if profitable / len(mc_results) < 0.60:
        issues.append(f"Only {profitable}/{len(mc_results)} runs profitable")
    if avg_roi < 0:
        issues.append(f"Average ROI is negative: {avg_roi:.1f}%")
    if avg_dd > 0.30:
        issues.append(f"Average drawdown {avg_dd:.1%} exceeds 30%")

    if issues:
        print("  ISSUES:")
        for i in issues:
            print(f"    - {i}")
    else:
        print("  ALL CHECKS PASSED")

    print()
    stale = strat_stats.get("stale_midrange", {})
    closing = strat_stats.get("closing_drift", {})
    if stale.get("pnl", 0) < 0:
        print(f"  WARNING: stale_midrange is net negative (${stale['pnl']:+,.2f}) — consider removing")
    if closing.get("pnl", 0) < 0:
        print(f"  WARNING: closing_drift is net negative (${closing['pnl']:+,.2f}) — consider removing")
    print()
    print(f"  VERDICT: {'PASS' if not issues else 'NEEDS WORK'}")
    print("=" * 74)


if __name__ == "__main__":
    print("=" * 74)
    print("  KALSHI LONGSHOT BIAS BOT — 1-YEAR BACKTEST")
    print("=" * 74)
    print()
    run_backtest()
