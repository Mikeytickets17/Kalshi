"""
Combined Strategy Backtest — 1 Year

Tests both strategies with honest loss modeling:

Strategy 1: LATENCY ARBITRAGE (Polymarket)
  - Monitors BTC/ETH 15-min contracts
  - Trades when CEX price diverges >3% from contract implied price
  - Win rate 85-95% depending on edge size and latency
  - Risk: edge compression over time as more bots enter

Strategy 2: TRUMP NEWS TRADING (Binance spot BTC)
  - Monitors Trump Truth Social for market-moving posts
  - Buys/sells BTC based on sentiment
  - Win rate 60-70% (news interpretation has uncertainty)
  - Risk: false signals, market already priced in, Trump posts at 3am

Both strategies modeled with:
  - Realistic fees (Polymarket: ~0, Binance: 0.1% taker)
  - Edge decay over time
  - Bad months (summer lull, low volatility = fewer arb windows)
  - Trump posting frequency varies (some weeks 20 posts, some weeks 3)
  - False signal rate for sentiment analysis
  - Slippage on execution
"""

import math
import random

INITIAL_BALANCE = 10_000.0
SIM_DAYS = 365

# --- Binance fee ---
BINANCE_FEE_PCT = 0.001  # 0.1% taker fee (each way = 0.2% round trip)

# --- Latency Arb Parameters ---
# Based on documented 0x8dxd-style performance
ARB = {
    "trades_per_day": (8, 25),        # 0x8dxd did ~60/day at peak, we model conservatively
    "base_win_rate": 0.92,            # 92% base, modified by edge size
    "avg_win_pct": 0.06,              # buy at ~50c, contract settles at ~56c avg (6% of position)
    "avg_loss_pct": 0.50,             # when wrong, contract settles worthless (lose 50% of position)
    "win_variance": 0.025,
    "loss_variance": 0.10,
    "position_size_pct": 0.03,        # 3% of portfolio per trade
    "max_position_usd": 500,
    "edge_decay_monthly": 0.003,      # window compressed from 12s to 2.7s over ~18 months
    "polymarket_fee": 0.0,            # no per-trade fee on Polymarket
}

# --- Trump News Parameters ---
# Based on documented news-driven trading (60-75% win rate)
TRUMP = {
    "posts_per_day_range": (0, 5),    # Trump posts 0-15/day, only 0-5 are market-relevant
    "market_relevant_pct": 0.35,      # ~35% of posts affect BTC
    "sentiment_accuracy": 0.72,       # Claude gets direction right 72% of the time
    "avg_btc_move_pct": 0.025,        # average BTC move on a relevant Trump post: 2.5%
    "move_variance": 0.015,           # can be 1% or 4%
    "execution_slippage_pct": 0.003,  # 0.3% slippage on market order
    "position_size_pct": 0.05,        # 5% of portfolio per Trump trade
    "max_position_usd": 500,
    "hold_minutes": 15,               # hold for 15 min then exit
    "false_signal_loss_pct": 0.015,   # when wrong, lose ~1.5% (BTC moved against us)
    "edge_decay_monthly": 0.002,      # others build similar bots
}

# --- Monthly regimes ---
REGIMES = {
    0: ("Jan - quiet", -0.01, 0.7),      # (name, arb_modifier, trump_post_multiplier)
    1: ("Feb - quiet", -0.01, 0.8),
    2: ("Mar - pickup", 0.01, 1.0),
    3: ("Apr - tariffs", 0.02, 1.5),      # tariff season = more Trump posts
    4: ("May - active", 0.01, 1.2),
    5: ("Jun - lull", -0.02, 0.6),
    6: ("Jul - slow", -0.03, 0.5),
    7: ("Aug - convention", 0.02, 1.8),   # political season
    8: ("Sep - debates", 0.03, 2.0),      # debate season = Trump posting a lot
    9: ("Oct - election", 0.04, 2.5),     # peak Trump posting
    10: ("Nov - resolution", 0.02, 1.5),
    11: ("Dec - wind down", -0.02, 0.5),
}


def run_single(seed: int, verbose: bool = False) -> dict:
    random.seed(seed)
    portfolio = INITIAL_BALANCE
    peak = INITIAL_BALANCE
    max_dd = 0.0

    arb_stats = {"trades": 0, "wins": 0, "gross_w": 0.0, "gross_l": 0.0, "pnl": 0.0, "fees": 0.0}
    trump_stats = {"trades": 0, "wins": 0, "gross_w": 0.0, "gross_l": 0.0, "pnl": 0.0, "fees": 0.0,
                   "posts_seen": 0, "posts_traded": 0}

    daily_pnls = []
    monthly_pnl = {}
    max_consec_loss = 0
    consec_loss = 0
    halted_days = 0

    for day in range(SIM_DAYS):
        month = day // 30
        regime_name, arb_mod, trump_mult = REGIMES.get(month % 12, ("", 0.0, 1.0))

        dd = (peak - portfolio) / peak if peak > 0 else 0
        if dd >= 0.40:
            halted_days += 1
            daily_pnls.append(0)
            monthly_pnl[month] = monthly_pnl.get(month, 0)
            continue

        daily_pnl = 0.0

        # ═══════════════════════════════════════
        # STRATEGY 1: LATENCY ARBITRAGE
        # ═══════════════════════════════════════
        arb_decay = ARB["edge_decay_monthly"] * (day / 30.0)
        arb_wr = max(ARB["base_win_rate"] - arb_decay + arb_mod, 0.60)
        num_arb = random.randint(*ARB["trades_per_day"])

        for _ in range(num_arb):
            pos = min(portfolio * ARB["position_size_pct"], ARB["max_position_usd"])
            pos = max(1.0, pos)

            if random.random() < arb_wr:
                # WIN: contract resolves in our favor
                wp = max(0.01, ARB["avg_win_pct"] + random.gauss(0, ARB["win_variance"]))
                raw_win = pos * wp
                fee = ARB["polymarket_fee"] * pos
                pnl = raw_win - fee
                arb_stats["wins"] += 1
                arb_stats["gross_w"] += raw_win
                arb_stats["fees"] += fee
                consec_loss = 0
            else:
                # LOSS: contract resolves against us
                lp = max(0.10, min(ARB["avg_loss_pct"] + random.gauss(0, ARB["loss_variance"]), 0.95))
                raw_loss = pos * lp
                pnl = -raw_loss
                arb_stats["gross_l"] += raw_loss
                consec_loss += 1
                max_consec_loss = max(max_consec_loss, consec_loss)

            arb_stats["trades"] += 1
            arb_stats["pnl"] += pnl
            daily_pnl += pnl
            portfolio += pnl

            # Daily loss circuit breaker
            if daily_pnl < -(INITIAL_BALANCE * 0.20):
                break

        # ═══════════════════════════════════════
        # STRATEGY 2: TRUMP NEWS TRADING
        # ═══════════════════════════════════════
        trump_decay = TRUMP["edge_decay_monthly"] * (day / 30.0)

        # How many posts does Trump make today?
        base_posts = random.randint(*TRUMP["posts_per_day_range"])
        total_posts = max(0, int(base_posts * trump_mult))
        trump_stats["posts_seen"] += total_posts

        for _ in range(total_posts):
            # Is this post market-relevant?
            if random.random() > TRUMP["market_relevant_pct"]:
                continue  # Not relevant, skip

            # Sentiment analysis: do we get the direction right?
            accuracy = max(TRUMP["sentiment_accuracy"] - trump_decay, 0.52)
            correct_direction = random.random() < accuracy

            pos = min(portfolio * TRUMP["position_size_pct"], TRUMP["max_position_usd"])
            pos = max(1.0, pos)

            # Fees: Binance 0.1% entry + 0.1% exit = 0.2% round trip
            round_trip_fee = pos * BINANCE_FEE_PCT * 2
            slippage_cost = pos * TRUMP["execution_slippage_pct"]

            if correct_direction:
                # We got the direction right — capture the BTC move
                move = abs(random.gauss(TRUMP["avg_btc_move_pct"], TRUMP["move_variance"]))
                move = max(0.005, move)  # at least 0.5%
                raw_win = pos * move
                pnl = raw_win - round_trip_fee - slippage_cost
                trump_stats["wins"] += 1
                trump_stats["gross_w"] += raw_win
                consec_loss = 0
            else:
                # Wrong direction — BTC moved against us
                move = abs(random.gauss(TRUMP["false_signal_loss_pct"], 0.008))
                move = max(0.003, move)
                raw_loss = pos * move
                pnl = -(raw_loss + round_trip_fee + slippage_cost)
                trump_stats["gross_l"] += raw_loss + round_trip_fee + slippage_cost
                consec_loss += 1
                max_consec_loss = max(max_consec_loss, consec_loss)

            trump_stats["trades"] += 1
            trump_stats["posts_traded"] += 1
            trump_stats["fees"] += round_trip_fee
            trump_stats["pnl"] += pnl
            daily_pnl += pnl
            portfolio += pnl

        # Track daily
        if portfolio > peak:
            peak = portfolio
        dd = (peak - portfolio) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

        daily_pnls.append(round(daily_pnl, 2))
        monthly_pnl[month] = monthly_pnl.get(month, 0) + daily_pnl

        if verbose and (day + 1) % 30 == 0:
            total_t = arb_stats["trades"] + trump_stats["trades"]
            total_w = arb_stats["wins"] + trump_stats["wins"]
            wr = total_w / max(total_t, 1) * 100
            print(
                f"  Day {day+1:>3} | ${portfolio:>10,.2f} | "
                f"Trades: {total_t:>5} (arb:{arb_stats['trades']} trump:{trump_stats['trades']}) | "
                f"WR: {wr:>5.1f}% | PnL: ${portfolio - INITIAL_BALANCE:>+9,.2f} | "
                f"DD: {dd:.1%} | {regime_name}"
            )

    # --- Compute results ---
    total_pnl = portfolio - INITIAL_BALANCE
    total_t = arb_stats["trades"] + trump_stats["trades"]
    total_w = arb_stats["wins"] + trump_stats["wins"]

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
        "total_trades": total_t, "total_wins": total_w,
        "wr": total_w / max(total_t, 1) * 100,
        "arb": arb_stats, "trump": trump_stats,
        "monthly_pnl": monthly_pnl, "daily_pnls": daily_pnls,
        "halted_days": halted_days, "max_consec_loss": max_consec_loss,
        "winning_days": sum(1 for d in daily_pnls if d > 0),
        "losing_days": sum(1 for d in daily_pnls if d < 0),
        "best_day": max(daily_pnls) if daily_pnls else 0,
        "worst_day": min(daily_pnls) if daily_pnls else 0,
    }


def main():
    print("=" * 78)
    print("  COMBINED STRATEGY BACKTEST — LATENCY ARB + TRUMP NEWS (1 YEAR)")
    print("=" * 78)
    print()

    # --- Detailed run ---
    r = run_single(42, verbose=True)
    arb = r["arb"]
    trump = r["trump"]

    print()
    print(f"  {'─'*74}")
    print(f"  PORTFOLIO SUMMARY")
    print(f"  {'─'*74}")
    print(f"  Initial Balance:       ${INITIAL_BALANCE:>12,.2f}")
    print(f"  Final Portfolio:       ${r['portfolio']:>12,.2f}")
    print(f"  Total P&L:             ${r['pnl']:>+12,.2f}")
    print(f"  ROI:                   {r['roi']:>+11.2f}%")
    print(f"  Peak Value:            ${r['peak']:>12,.2f}")
    print(f"  Max Drawdown:          {r['max_dd']:>11.2%}")
    print(f"  Sharpe Ratio:          {r['sharpe']:>11.2f}")
    print(f"  Halted Days:           {r['halted_days']:>8}")
    print(f"  Best Day:              ${r['best_day']:>+11,.2f}")
    print(f"  Worst Day:             ${r['worst_day']:>+11,.2f}")
    print(f"  Max Consec Losses:     {r['max_consec_loss']:>8}")
    print()
    print(f"  Winning Days:          {r['winning_days']:>8} ({r['winning_days']/365*100:.0f}%)")
    print(f"  Losing Days:           {r['losing_days']:>8} ({r['losing_days']/365*100:.0f}%)")
    print()

    print(f"  {'─'*74}")
    print(f"  STRATEGY 1: LATENCY ARBITRAGE (Polymarket)")
    print(f"  {'─'*74}")
    arb_wr = arb["wins"] / max(arb["trades"], 1) * 100
    arb_ev = arb["pnl"] / max(arb["trades"], 1)
    arb_pf = arb["gross_w"] / max(arb["gross_l"], 0.01)
    print(f"  Trades:                {arb['trades']:>8,}")
    print(f"  Wins:                  {arb['wins']:>8,}")
    print(f"  Win Rate:              {arb_wr:>10.1f}%")
    print(f"  Gross Wins:            ${arb['gross_w']:>12,.2f}")
    print(f"  Gross Losses:          ${arb['gross_l']:>12,.2f}")
    print(f"  Fees:                  ${arb['fees']:>12,.2f}")
    print(f"  Net P&L:               ${arb['pnl']:>+12,.2f}")
    print(f"  EV per Trade:          ${arb_ev:>+11.2f}")
    print(f"  Profit Factor:         {arb_pf:>11.2f}")
    print()

    print(f"  {'─'*74}")
    print(f"  STRATEGY 2: TRUMP NEWS TRADING (Binance BTC)")
    print(f"  {'─'*74}")
    trump_wr = trump["wins"] / max(trump["trades"], 1) * 100
    trump_ev = trump["pnl"] / max(trump["trades"], 1)
    trump_pf = trump["gross_w"] / max(trump["gross_l"], 0.01)
    print(f"  Posts Seen:            {trump['posts_seen']:>8,}")
    print(f"  Posts Traded:          {trump['posts_traded']:>8,}")
    print(f"  Trades:                {trump['trades']:>8,}")
    print(f"  Wins:                  {trump['wins']:>8,}")
    print(f"  Win Rate:              {trump_wr:>10.1f}%")
    print(f"  Gross Wins:            ${trump['gross_w']:>12,.2f}")
    print(f"  Gross Losses:          ${trump['gross_l']:>12,.2f}")
    print(f"  Fees (Binance):        ${trump['fees']:>12,.2f}")
    print(f"  Net P&L:               ${trump['pnl']:>+12,.2f}")
    print(f"  EV per Trade:          ${trump_ev:>+11.2f}")
    print(f"  Profit Factor:         {trump_pf:>11.2f}")
    print()

    print(f"  {'─'*74}")
    print(f"  MONTHLY RETURNS")
    print(f"  {'─'*74}")
    cum = 0
    for m in sorted(r["monthly_pnl"].keys()):
        p = r["monthly_pnl"][m]
        cum += p
        regime = REGIMES.get(m % 12, ("", 0, 0))[0]
        bar_len = int(abs(p) / 40)
        bar = ("█" * min(bar_len, 40)) if p >= 0 else ("░" * min(bar_len, 40))
        sign = "+" if p >= 0 else "-"
        print(f"  Month {m+1:>2}: ${p:>+9,.2f} (cum: ${cum:>+10,.2f}) {bar}  {regime}")
    print()

    # --- Monte Carlo ---
    NUM_SEEDS = 50
    print(f"  {'─'*74}")
    print(f"  MONTE CARLO: {NUM_SEEDS} SEEDS")
    print(f"  {'─'*74}")
    print()

    results = [r]
    for seed in range(1, NUM_SEEDS):
        if seed == 42:
            continue
        results.append(run_single(seed))

    rois = [x["roi"] for x in results]
    dds = [x["max_dd"] for x in results]
    sharpes = [x["sharpe"] for x in results]
    profitable = sum(1 for x in rois if x > 0)

    def p(lst, pct):
        return sorted(lst)[int(len(lst) * pct)]
    def avg(lst):
        return sum(lst) / len(lst)

    print(f"  Profitable Runs:    {profitable}/{len(results)} ({profitable/len(results)*100:.0f}%)")
    print()
    print(f"  {'Metric':<18s} {'Mean':>10s} {'Median':>10s} {'P10':>10s} {'P90':>10s} {'Worst':>10s} {'Best':>10s}")
    print(f"  {'-'*18} {'-'*10} {'-'*10} {'-'*10} {'-'*10} {'-'*10} {'-'*10}")
    print(f"  {'ROI %':<18s} {avg(rois):>+9.1f}% {p(rois,0.5):>+9.1f}% {p(rois,0.1):>+9.1f}% {p(rois,0.9):>+9.1f}% {min(rois):>+9.1f}% {max(rois):>+9.1f}%")
    print(f"  {'Max Drawdown':<18s} {avg(dds):>9.1%}  {p(dds,0.5):>9.1%}  {p(dds,0.1):>9.1%}  {p(dds,0.9):>9.1%}  {min(dds):>9.1%}  {max(dds):>9.1%}")
    print(f"  {'Sharpe':<18s} {avg(sharpes):>10.2f} {p(sharpes,0.5):>10.2f} {p(sharpes,0.1):>10.2f} {p(sharpes,0.9):>10.2f} {min(sharpes):>10.2f} {max(sharpes):>10.2f}")
    print()

    # Breakdown by strategy across all seeds
    arb_pnls = [x["arb"]["pnl"] for x in results]
    trump_pnls = [x["trump"]["pnl"] for x in results]
    print(f"  Strategy contribution across {len(results)} seeds:")
    print(f"  Latency Arb avg P&L:   ${avg(arb_pnls):>+10,.2f}  (min: ${min(arb_pnls):>+10,.2f}, max: ${max(arb_pnls):>+10,.2f})")
    print(f"  Trump News avg P&L:    ${avg(trump_pnls):>+10,.2f}  (min: ${min(trump_pnls):>+10,.2f}, max: ${max(trump_pnls):>+10,.2f})")
    print()

    # ROI distribution
    print(f"  {'─'*74}")
    print(f"  ROI DISTRIBUTION")
    print(f"  {'─'*74}")
    buckets = {}
    for roi in rois:
        b = int(roi // 20) * 20
        buckets[b] = buckets.get(b, 0) + 1
    for b in sorted(buckets.keys()):
        bar = "█" * buckets[b]
        print(f"  {b:>+5d}% to {b+20:>+5d}%: {bar} ({buckets[b]})")
    print()

    # Verdict
    issues = []
    if profitable / len(results) < 0.60:
        issues.append(f"Only {profitable}/{len(results)} runs profitable")
    if avg(rois) < 0:
        issues.append(f"Average ROI is negative: {avg(rois):.1f}%")
    if avg(dds) > 0.35:
        issues.append(f"Average drawdown {avg(dds):.1%} exceeds 35%")
    if avg(arb_pnls) < 0:
        issues.append(f"Latency arb is net negative on average (${avg(arb_pnls):+,.2f})")
    if avg(trump_pnls) < 0:
        issues.append(f"Trump news is net negative on average (${avg(trump_pnls):+,.2f})")

    print(f"  {'─'*74}")
    if issues:
        print("  ISSUES:")
        for i in issues:
            print(f"    ⚠ {i}")
    else:
        print("  ALL CHECKS PASSED")

    print()
    print(f"  VERDICT: {'PASS' if not issues else 'NEEDS WORK'}")
    print("=" * 78)


if __name__ == "__main__":
    main()
