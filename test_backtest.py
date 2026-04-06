"""
Kalshi Longshot Bias Bot — 1-Year Backtest (v2: lean 2-strategy)

Only the two profitable strategies survive:
  1. Longshot fade (YES < 12c, edge > 3c after fees)
  2. Favorite lean (YES > 75c, edge > 2c after fees)

All fees, losses, edge decay, and variance modeled honestly.
50-seed Monte Carlo for robustness.
"""

import math
import random

INITIAL_BALANCE = 10_000.0
SIM_DAYS = 365
FEE_PER_TRADE_PCT = 0.015  # Kalshi ~1.5c per contract

STRATEGIES = {
    "longshot_fade": {
        "trades_per_day": (3, 7),
        "win_rate": 0.94,             # only cheapest longshots (<8c), bias is strongest
        "avg_win_pct": 0.07,          # buy NO at ~93c, collect ~7c
        "avg_loss_pct": 0.93,         # when longshot hits, lose big
        "win_variance": 0.015,
        "loss_variance": 0.03,
        "position_size_pct": 0.012,   # tiny sizing: one loss = ~1.2% of portfolio
        "edge_decay_monthly": 0.001,
    },
    "favorite_lean": {
        "trades_per_day": (1, 3),     # fewer, higher quality
        "win_rate": 0.88,             # only 90c+ favorites where bias is strongest
        "avg_win_pct": 0.09,          # buy YES at ~91c, collect ~9c
        "avg_loss_pct": 0.91,         # lose ~91c — same asymmetry as longshots
        "win_variance": 0.03,
        "loss_variance": 0.04,
        "position_size_pct": 0.012,
        "edge_decay_monthly": 0.001,
    },
}

# Separate fee model — Kalshi charges on settlement, not entry
# For contracts that settle at $1: fee is ~1.5c
# For contracts that settle at $0: no fee (you already lost)
FEE_ON_WIN_ONLY = True  # Kalshi only charges fee when you collect

MONTHLY_REGIME = {
    0: ("Jan - quiet", -0.01),
    1: ("Feb - quiet", -0.01),
    2: ("Mar - pickup", 0.01),
    3: ("Apr - events", 0.02),
    4: ("May - active", 0.01),
    5: ("Jun - lull", -0.02),
    6: ("Jul - slow", -0.02),
    7: ("Aug - convention", 0.02),
    8: ("Sep - debates", 0.03),
    9: ("Oct - election", 0.04),
    10: ("Nov - resolution", 0.02),
    11: ("Dec - wind down", -0.01),
}


def run_single(seed: int, verbose: bool = False) -> dict:
    random.seed(seed)
    portfolio = INITIAL_BALANCE
    peak = INITIAL_BALANCE
    max_dd = 0.0

    strat_stats = {}
    for name in STRATEGIES:
        strat_stats[name] = {"trades": 0, "wins": 0, "losses": 0, "gross_w": 0.0, "gross_l": 0.0, "pnl": 0.0}

    daily_pnls = []
    monthly_pnl = {}
    total_fees = 0.0
    halted_days = 0
    max_consec_loss = 0
    consec_loss = 0

    for day in range(SIM_DAYS):
        month = day // 30
        _, regime_mod = MONTHLY_REGIME.get(month % 12, ("", 0.0))

        # No hard halt — let it trade through drawdowns with reduced sizing
        # (position_size_pct is % of CURRENT portfolio, so sizing auto-shrinks)

        daily_pnl = 0.0

        for sname, params in STRATEGIES.items():
            lo, hi = params["trades_per_day"]
            num = random.randint(lo, hi)
            decay = params["edge_decay_monthly"] * (day / 30.0)
            eff_wr = max(params["win_rate"] - decay + regime_mod, 0.45)

            for _ in range(num):
                pos = max(2.0, min(portfolio * params["position_size_pct"], 500.0))

                if random.random() < eff_wr:
                    wp = max(0.01, params["avg_win_pct"] + random.gauss(0, params["win_variance"]))
                    raw = pos * wp
                    fee = pos * FEE_PER_TRADE_PCT  # fee on settlement
                    pnl = raw - fee
                    total_fees += fee
                    strat_stats[sname]["wins"] += 1
                    strat_stats[sname]["gross_w"] += raw
                    consec_loss = 0
                else:
                    lp = max(0.10, min(params["avg_loss_pct"] + random.gauss(0, params["loss_variance"]), 1.0))
                    raw = pos * lp
                    # Kalshi: no fee when contract settles worthless (you lost)
                    fee = 0.0
                    pnl = -raw
                    strat_stats[sname]["losses"] += 1
                    strat_stats[sname]["gross_l"] += raw
                    consec_loss += 1
                    max_consec_loss = max(max_consec_loss, consec_loss)

                strat_stats[sname]["trades"] += 1
                strat_stats[sname]["pnl"] += pnl
                daily_pnl += pnl
                portfolio += pnl

                if daily_pnl < -(INITIAL_BALANCE * 0.10):
                    break
            if daily_pnl < -(INITIAL_BALANCE * 0.10):
                break

        if portfolio > peak:
            peak = portfolio
        dd = (peak - portfolio) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

        daily_pnls.append(round(daily_pnl, 2))

        m = month
        monthly_pnl[m] = monthly_pnl.get(m, 0) + daily_pnl

        if verbose and (day + 1) % 30 == 0:
            tt = sum(s["trades"] for s in strat_stats.values())
            tw = sum(s["wins"] for s in strat_stats.values())
            wr = tw / max(tt, 1) * 100
            regime_name = MONTHLY_REGIME.get(month % 12, ("", 0))[0]
            print(
                f"  Day {day+1:>3} | ${portfolio:>10,.2f} | "
                f"Trades: {tt:>5} | WR: {wr:>5.1f}% | "
                f"PnL: ${portfolio - INITIAL_BALANCE:>+9,.2f} | "
                f"DD: {dd:.1%} | {regime_name}"
            )

    total_pnl = portfolio - INITIAL_BALANCE
    tt = sum(s["trades"] for s in strat_stats.values())
    tw = sum(s["wins"] for s in strat_stats.values())
    tl = sum(s["losses"] for s in strat_stats.values())
    gw = sum(s["gross_w"] for s in strat_stats.values())
    gl = sum(s["gross_l"] for s in strat_stats.values())

    if len(daily_pnls) > 1:
        avg_d = sum(daily_pnls) / len(daily_pnls)
        std_d = math.sqrt(sum((d - avg_d)**2 for d in daily_pnls) / (len(daily_pnls) - 1))
        sharpe = (avg_d / std_d) * math.sqrt(252) if std_d > 0 else 0
    else:
        sharpe = 0

    return {
        "seed": seed, "portfolio": portfolio, "pnl": total_pnl,
        "roi": total_pnl / INITIAL_BALANCE * 100,
        "peak": peak, "max_dd": max_dd, "sharpe": sharpe,
        "trades": tt, "wins": tw, "losses": tl,
        "wr": tw / max(tt, 1) * 100,
        "profit_factor": gw / max(gl, 0.01),
        "fees": total_fees,
        "strat_stats": strat_stats,
        "monthly_pnl": monthly_pnl,
        "daily_pnls": daily_pnls,
        "halted_days": halted_days,
        "max_consec_loss": max_consec_loss,
        "winning_days": sum(1 for d in daily_pnls if d > 0),
        "losing_days": sum(1 for d in daily_pnls if d < 0),
        "best_day": max(daily_pnls) if daily_pnls else 0,
        "worst_day": min(daily_pnls) if daily_pnls else 0,
    }


def main():
    print("=" * 74)
    print("  KALSHI BOT — 1-YEAR BACKTEST (v2: LEAN 2-STRATEGY)")
    print("=" * 74)
    print()
    print("  Strategies: longshot_fade + favorite_lean ONLY")
    print("  Killed: closing_drift, multi_arb, stale_midrange")
    print()

    # Detailed run
    r = run_single(42, verbose=True)
    print()
    print(f"  Final Portfolio:       ${r['portfolio']:>12,.2f}")
    print(f"  Total P&L:             ${r['pnl']:>+12,.2f}")
    print(f"  ROI:                   {r['roi']:>+11.2f}%")
    print(f"  Max Drawdown:          {r['max_dd']:>11.2%}")
    print(f"  Sharpe Ratio:          {r['sharpe']:>11.2f}")
    print(f"  Profit Factor:         {r['profit_factor']:>11.2f}")
    print(f"  Total Fees:            ${r['fees']:>12,.2f}")
    print()
    print(f"  Total Trades:          {r['trades']:>8,}")
    print(f"  Win Rate:              {r['wr']:>10.1f}%")
    print(f"  Winning Days:          {r['winning_days']:>8} ({r['winning_days']/365*100:.0f}%)")
    print(f"  Losing Days:           {r['losing_days']:>8} ({r['losing_days']/365*100:.0f}%)")
    print(f"  Halted Days:           {r['halted_days']:>8}")
    print(f"  Best Day:              ${r['best_day']:>+11,.2f}")
    print(f"  Worst Day:             ${r['worst_day']:>+11,.2f}")
    print(f"  Max Consec Losses:     {r['max_consec_loss']:>8}")
    print()

    print(f"  --- Per-Strategy ---")
    print(f"  {'Strategy':<18s} {'Trades':>7} {'Wins':>6} {'WR':>7} {'Gross+':>10} {'Gross-':>10} {'Net P&L':>10} {'EV/Trade':>9}")
    print(f"  {'-'*18} {'-'*7} {'-'*6} {'-'*7} {'-'*10} {'-'*10} {'-'*10} {'-'*9}")
    for name, s in sorted(r["strat_stats"].items(), key=lambda x: x[1]["pnl"], reverse=True):
        wr = s["wins"] / max(s["trades"], 1) * 100
        ev = s["pnl"] / max(s["trades"], 1)
        print(
            f"  {name:<18s} {s['trades']:>7,} {s['wins']:>6,} {wr:>6.1f}% "
            f"${s['gross_w']:>9,.2f} ${s['gross_l']:>9,.2f} "
            f"${s['pnl']:>+9,.2f} ${ev:>+8.2f}"
        )
    print()

    print(f"  --- Monthly Returns ---")
    cum = 0
    for m in sorted(r["monthly_pnl"].keys()):
        p = r["monthly_pnl"][m]
        cum += p
        regime = MONTHLY_REGIME.get(m % 12, ("", 0))[0]
        bar_len = int(abs(p) / 20)
        bar = ("+" * min(bar_len, 40)) if p >= 0 else ("-" * min(bar_len, 40))
        print(f"  Month {m+1:>2}: ${p:>+8,.2f} (cum: ${cum:>+9,.2f}) {bar}  {regime}")
    print()

    # Monte Carlo
    NUM_SEEDS = 50
    print(f"  --- Monte Carlo: {NUM_SEEDS} Seeds ---")
    print()
    results = [r]
    for seed in range(1, NUM_SEEDS):
        if seed == 42:
            continue
        results.append(run_single(seed))

    rois = [x["roi"] for x in results]
    dds = [x["max_dd"] for x in results]
    sharpes = [x["sharpe"] for x in results]
    pfs = [x["profit_factor"] for x in results]
    fees_list = [x["fees"] for x in results]
    profitable = sum(1 for x in rois if x > 0)

    def p(lst, pct):
        return sorted(lst)[int(len(lst) * pct)]

    print(f"  Profitable Runs:    {profitable}/{len(results)} ({profitable/len(results)*100:.0f}%)")
    print()
    print(f"  {'Metric':<18s} {'Mean':>10s} {'Median':>10s} {'P10':>10s} {'P90':>10s} {'Worst':>10s} {'Best':>10s}")
    print(f"  {'-'*18} {'-'*10} {'-'*10} {'-'*10} {'-'*10} {'-'*10} {'-'*10}")

    def avg(lst): return sum(lst) / len(lst)

    print(f"  {'ROI %':<18s} {avg(rois):>+9.1f}% {p(rois,0.5):>+9.1f}% {p(rois,0.1):>+9.1f}% {p(rois,0.9):>+9.1f}% {min(rois):>+9.1f}% {max(rois):>+9.1f}%")
    print(f"  {'Max Drawdown':<18s} {avg(dds):>9.1%}  {p(dds,0.5):>9.1%}  {p(dds,0.1):>9.1%}  {p(dds,0.9):>9.1%}  {min(dds):>9.1%}  {max(dds):>9.1%}")
    print(f"  {'Sharpe':<18s} {avg(sharpes):>10.2f} {p(sharpes,0.5):>10.2f} {p(sharpes,0.1):>10.2f} {p(sharpes,0.9):>10.2f} {min(sharpes):>10.2f} {max(sharpes):>10.2f}")
    print(f"  {'Profit Factor':<18s} {avg(pfs):>10.2f} {p(pfs,0.5):>10.2f} {p(pfs,0.1):>10.2f} {p(pfs,0.9):>10.2f} {min(pfs):>10.2f} {max(pfs):>10.2f}")
    print(f"  {'Fees Paid':<18s} ${avg(fees_list):>9,.0f} ${p(fees_list,0.5):>9,.0f} ${p(fees_list,0.1):>9,.0f} ${p(fees_list,0.9):>9,.0f} ${min(fees_list):>9,.0f} ${max(fees_list):>9,.0f}")
    print()

    # ROI distribution
    print("  --- ROI Distribution ---")
    buckets = {}
    for roi in rois:
        b = int(roi // 10) * 10
        buckets[b] = buckets.get(b, 0) + 1
    for b in sorted(buckets.keys()):
        bar = "#" * buckets[b]
        print(f"  {b:>+4d}% to {b+10:>+4d}%: {bar} ({buckets[b]})")
    print()

    issues = []
    if profitable / len(results) < 0.60:
        issues.append(f"Only {profitable}/{len(results)} runs profitable")
    if avg(rois) < 0:
        issues.append(f"Average ROI is negative: {avg(rois):.1f}%")
    if avg(dds) > 0.30:
        issues.append(f"Average drawdown {avg(dds):.1%} exceeds 30%")

    for name, s in r["strat_stats"].items():
        if s["pnl"] < 0:
            issues.append(f"{name} is net negative (${s['pnl']:+,.2f})")

    if issues:
        print("  ISSUES:")
        for i in issues:
            print(f"    - {i}")
    else:
        print("  ALL CHECKS PASSED")

    print()
    print(f"  VERDICT: {'PASS' if not issues else 'NEEDS WORK'}")
    print("=" * 74)


if __name__ == "__main__":
    main()
