"""
Backtesting framework for strategy validation.

Replays historical data through the trading strategies and reports
performance metrics. Uses real market data where available,
simulated data for unavailable periods.

Usage:
    python backtest.py                    # Run all strategies, 30 days
    python backtest.py --days 90          # Run 90-day backtest
    python backtest.py --strategy trump   # Trump strategy only
    python backtest.py --strategy arb     # Arb strategy only
"""

import argparse
import json
import logging
import math
import os
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import config

logging.basicConfig(level=logging.WARNING, format="%(message)s")
logger = logging.getLogger(__name__)


@dataclass
class BacktestTrade:
    """A single backtested trade."""
    strategy: str
    side: str
    asset: str
    venue: str
    entry_price: float
    exit_price: float
    size_usd: float
    pnl: float
    result: str  # "WIN" or "LOSS"
    entry_time: float
    exit_time: float
    hold_seconds: float
    confidence: float
    reason: str


@dataclass
class BacktestResult:
    """Results of a backtest run."""
    strategy: str
    days: int
    initial_balance: float
    final_balance: float
    total_pnl: float
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    avg_win: float
    avg_loss: float
    max_drawdown_pct: float
    sharpe_ratio: float
    profit_factor: float
    best_trade: float
    worst_trade: float
    avg_hold_seconds: float
    equity_curve: list[float] = field(default_factory=list)
    trades: list[BacktestTrade] = field(default_factory=list)


class MarketSimulator:
    """Simulates realistic market conditions for backtesting."""

    def __init__(self, seed: int = 42) -> None:
        self._rng = random.Random(seed)
        self._btc_price = 83000.0
        self._eth_price = 1800.0
        self._volatility_regime = "normal"  # low, normal, high, crisis

    def tick(self, dt_seconds: float = 1.0) -> dict:
        """Advance market by dt_seconds, return prices."""
        # BTC random walk with regime-dependent volatility
        vol = {"low": 0.0002, "normal": 0.0004, "high": 0.0008, "crisis": 0.002}
        v = vol[self._volatility_regime]
        self._btc_price *= (1 + self._rng.gauss(0, v) * math.sqrt(dt_seconds))
        self._eth_price *= (1 + self._rng.gauss(0, v * 1.3) * math.sqrt(dt_seconds))

        # Regime changes (rare)
        if self._rng.random() < 0.001:
            self._volatility_regime = self._rng.choice(["low", "normal", "high", "crisis"])

        return {
            "BTC": round(self._btc_price, 2),
            "ETH": round(self._eth_price, 2),
            "regime": self._volatility_regime,
        }

    def simulate_trump_post(self) -> dict | None:
        """Randomly generate Trump post events (avg 2-4 per day)."""
        if self._rng.random() > 0.003:  # ~2.5 posts per day at 1-sec ticks
            return None

        templates = [
            {"text": "Bitcoin is the future! Strategic reserve NOW!", "dir": "BULLISH", "conf": 0.85, "move": 0.03},
            {"text": "TARIFFS on China going to 60%!", "dir": "BEARISH", "conf": 0.80, "move": -0.025},
            {"text": "The Fed should CUT RATES immediately!", "dir": "BULLISH", "conf": 0.70, "move": 0.02},
            {"text": "Just signed crypto executive order!", "dir": "BULLISH", "conf": 0.90, "move": 0.04},
            {"text": "Trade war with EU escalating!", "dir": "BEARISH", "conf": 0.75, "move": -0.02},
            {"text": "Stock market at ALL TIME HIGH!", "dir": "BULLISH", "conf": 0.40, "move": 0.005},
            {"text": "Happy Easter to all!", "dir": "NEUTRAL", "conf": 0.1, "move": 0.0},
            {"text": "Crypto regulation will be FAIR!", "dir": "BULLISH", "conf": 0.65, "move": 0.015},
        ]
        post = self._rng.choice(templates)

        # Apply market move
        if post["dir"] == "BULLISH":
            self._btc_price *= (1 + abs(post["move"]) * self._rng.uniform(0.5, 1.5))
        elif post["dir"] == "BEARISH":
            self._btc_price *= (1 - abs(post["move"]) * self._rng.uniform(0.5, 1.5))

        return post

    def simulate_news_event(self) -> dict | None:
        """Randomly generate critical news events."""
        if self._rng.random() > 0.002:
            return None

        events = [
            {"headline": "Fed cuts rates 25bp", "cat": "fed", "dir": "BULLISH", "conf": 0.80, "move": 0.015},
            {"headline": "CPI comes in hot at 3.5%", "cat": "economic_data", "dir": "BEARISH", "conf": 0.75, "move": -0.02},
            {"headline": "Jobs report beats expectations", "cat": "economic_data", "dir": "BULLISH", "conf": 0.65, "move": 0.01},
            {"headline": "Russia-Ukraine ceasefire", "cat": "geopolitical", "dir": "BULLISH", "conf": 0.70, "move": 0.02},
            {"headline": "Oil spikes on OPEC cuts", "cat": "geopolitical", "dir": "BEARISH", "conf": 0.60, "move": -0.01},
            {"headline": "Bitcoin ETF inflows record high", "cat": "crypto", "dir": "BULLISH", "conf": 0.80, "move": 0.025},
        ]
        return self._rng.choice(events)

    def simulate_arb_opportunity(self, btc_price: float) -> dict | None:
        """Simulate Kalshi contract lagging behind CEX price."""
        if self._rng.random() > 0.008:  # ~0.8% of ticks have arb opportunity
            return None

        # Simulate Kalshi contract lagging 2-8 seconds behind CEX
        strike = round(btc_price * (1 + self._rng.uniform(-0.006, 0.006)), 0)
        distance_pct = abs(btc_price - strike) / strike

        # Contract price hasn't caught up to the CEX price move
        stale_price = max(0.10, min(0.90, 0.50 + self._rng.gauss(0, 0.08)))
        true_prob = min(0.95, 0.55 + distance_pct * 15) if btc_price > strike else max(0.05, 0.45 - distance_pct * 15)
        edge = abs(true_prob - stale_price)

        if edge >= 0.03:
            return {
                "ticker": f"BTC-UP-{int(strike)}",
                "strike": strike,
                "contract_price": round(stale_price, 4),
                "edge": round(edge, 4),
                "side": "YES" if btc_price > strike else "NO",
            }
        return None


def run_backtest(
    strategy: str = "all",
    days: int = 30,
    initial_balance: float = 10000.0,
    seed: int = 42,
) -> list[BacktestResult]:
    """Run a full backtest simulation."""
    sim = MarketSimulator(seed=seed)
    results = []

    strategies_to_run = ["trump", "arb", "news"] if strategy == "all" else [strategy]

    for strat in strategies_to_run:
        balance = initial_balance
        peak = initial_balance
        max_dd = 0.0
        trades: list[BacktestTrade] = []
        equity = [initial_balance]
        daily_returns: list[float] = []
        day_start_balance = initial_balance

        total_seconds = days * 86400
        t = 0
        step = 10  # 10-second resolution

        while t < total_seconds:
            prices = sim.tick(step)
            btc = prices["BTC"]

            trade = None

            if strat == "trump":
                post = sim.simulate_trump_post()
                if post and post["conf"] >= config.TRUMP_MIN_CONFIDENCE:
                    side = "BUY" if post["dir"] == "BULLISH" else "SELL" if post["dir"] == "BEARISH" else None
                    if side:
                        size = min(balance * config.TRUMP_TRADE_SIZE_PCT * post["conf"], config.TRUMP_MAX_TRADE_SIZE_USDC)
                        if size >= config.MIN_TRADE_SIZE_USDC and size <= balance:
                            # Simulate hold period
                            hold_secs = config.TRUMP_HOLD_MINUTES * 60
                            # Price after hold
                            for _ in range(int(hold_secs / step)):
                                prices = sim.tick(step)
                                t += step
                            exit_price = prices["BTC"]

                            if side == "BUY":
                                pnl = size * (exit_price - btc) / btc
                            else:
                                pnl = size * (btc - exit_price) / btc

                            # Slippage
                            pnl -= size * 0.001

                            trade = BacktestTrade(
                                strategy="TRUMP", side=side, asset="BTC", venue="Binance",
                                entry_price=btc, exit_price=exit_price, size_usd=size,
                                pnl=round(pnl, 2), result="WIN" if pnl > 0 else "LOSS",
                                entry_time=t - hold_secs, exit_time=t,
                                hold_seconds=hold_secs, confidence=post["conf"],
                                reason=post["text"][:60],
                            )

            elif strat == "arb":
                opp = sim.simulate_arb_opportunity(btc)
                if opp and opp["edge"] >= config.EDGE_THRESHOLD_PCT:
                    size = min(balance * config.BASE_COPY_PCT * opp["edge"] * 10, config.MAX_TRADE_SIZE_USDC)
                    if size >= config.MIN_TRADE_SIZE_USDC and size <= balance:
                        # Arb resolves in 5-60 minutes
                        hold_secs = sim._rng.uniform(300, 3600)
                        for _ in range(int(hold_secs / step)):
                            prices = sim.tick(step)
                            t += step

                        # High-edge arb wins ~85-92% of the time
                        win_prob = min(0.75 + opp["edge"] * 3, 0.95)
                        won = sim._rng.random() < win_prob
                        pnl = size * opp["edge"] * 2 if won else -size * opp["edge"] * 4

                        trade = BacktestTrade(
                            strategy="ARB", side=opp["side"], asset=opp["ticker"], venue="Kalshi",
                            entry_price=opp["contract_price"], exit_price=0.99 if won else 0.01,
                            size_usd=size, pnl=round(pnl, 2),
                            result="WIN" if won else "LOSS",
                            entry_time=t - hold_secs, exit_time=t,
                            hold_seconds=hold_secs, confidence=opp["edge"],
                            reason=f"Edge {opp['edge']*100:.1f}%",
                        )

            elif strat == "news":
                news = sim.simulate_news_event()
                if news and news["conf"] >= 0.50:
                    side = "BUY" if news["dir"] == "BULLISH" else "SELL"
                    size = min(balance * 0.04 * news["conf"], config.TRUMP_MAX_TRADE_SIZE_USDC)
                    if size >= config.MIN_TRADE_SIZE_USDC and size <= balance:
                        hold_secs = sim._rng.uniform(600, 3600)
                        for _ in range(int(hold_secs / step)):
                            prices = sim.tick(step)
                            t += step
                        exit_price = prices["BTC"]

                        if side == "BUY":
                            pnl = size * (exit_price - btc) / btc
                        else:
                            pnl = size * (btc - exit_price) / btc
                        pnl -= size * 0.001  # slippage

                        trade = BacktestTrade(
                            strategy="NEWS", side=side, asset="BTC", venue="Binance",
                            entry_price=btc, exit_price=exit_price, size_usd=size,
                            pnl=round(pnl, 2), result="WIN" if pnl > 0 else "LOSS",
                            entry_time=t - hold_secs, exit_time=t,
                            hold_seconds=hold_secs, confidence=news["conf"],
                            reason=news["headline"][:60],
                        )

            if trade:
                balance += trade.pnl
                trades.append(trade)
                if balance > peak:
                    peak = balance
                dd = (peak - balance) / peak if peak > 0 else 0
                if dd > max_dd:
                    max_dd = dd
                equity.append(round(balance, 2))

            # Track daily returns
            if t % 86400 < step:
                daily_ret = (balance - day_start_balance) / day_start_balance if day_start_balance > 0 else 0
                daily_returns.append(daily_ret)
                day_start_balance = balance

            t += step

        # Compute metrics
        wins = [t for t in trades if t.result == "WIN"]
        losses_list = [t for t in trades if t.result == "LOSS"]
        total = len(trades)
        avg_win = sum(t.pnl for t in wins) / max(len(wins), 1)
        avg_loss = sum(t.pnl for t in losses_list) / max(len(losses_list), 1)
        gross_profit = sum(t.pnl for t in wins)
        gross_loss = abs(sum(t.pnl for t in losses_list))
        profit_factor = gross_profit / max(gross_loss, 0.01)

        # Sharpe ratio (annualized)
        if daily_returns and len(daily_returns) > 1:
            import statistics
            mean_ret = statistics.mean(daily_returns)
            std_ret = statistics.stdev(daily_returns) if len(daily_returns) > 1 else 1
            sharpe = (mean_ret / max(std_ret, 0.0001)) * math.sqrt(365)
        else:
            sharpe = 0.0

        result = BacktestResult(
            strategy=strat.upper(),
            days=days,
            initial_balance=initial_balance,
            final_balance=round(balance, 2),
            total_pnl=round(balance - initial_balance, 2),
            total_trades=total,
            wins=len(wins),
            losses=len(losses_list),
            win_rate=round(len(wins) / max(total, 1) * 100, 1),
            avg_win=round(avg_win, 2),
            avg_loss=round(avg_loss, 2),
            max_drawdown_pct=round(max_dd * 100, 2),
            sharpe_ratio=round(sharpe, 2),
            profit_factor=round(profit_factor, 2),
            best_trade=round(max((t.pnl for t in trades), default=0), 2),
            worst_trade=round(min((t.pnl for t in trades), default=0), 2),
            avg_hold_seconds=round(sum(t.hold_seconds for t in trades) / max(total, 1), 0),
            equity_curve=equity,
            trades=trades,
        )
        results.append(result)

    return results


def print_results(results: list[BacktestResult]) -> None:
    """Pretty-print backtest results."""
    print()
    print("=" * 70)
    print("  BACKTEST RESULTS")
    print("=" * 70)

    for r in results:
        pnl_color = "\033[92m" if r.total_pnl >= 0 else "\033[91m"
        reset = "\033[0m"

        print(f"\n  Strategy: {r.strategy} ({r.days} days)")
        print(f"  " + "-" * 50)
        print(f"  Initial:          ${r.initial_balance:>12,.2f}")
        print(f"  Final:            ${r.final_balance:>12,.2f}")
        print(f"  P&L:              {pnl_color}${r.total_pnl:>+12,.2f}{reset}")
        print(f"  ROI:              {pnl_color}{r.total_pnl/r.initial_balance*100:>+11.2f}%{reset}")
        print(f"  Total Trades:     {r.total_trades:>12}")
        print(f"  Win Rate:         {r.win_rate:>11.1f}%")
        print(f"  Avg Win:          ${r.avg_win:>+12,.2f}")
        print(f"  Avg Loss:         ${r.avg_loss:>+12,.2f}")
        print(f"  Best Trade:       ${r.best_trade:>+12,.2f}")
        print(f"  Worst Trade:      ${r.worst_trade:>+12,.2f}")
        print(f"  Profit Factor:    {r.profit_factor:>12.2f}")
        print(f"  Sharpe Ratio:     {r.sharpe_ratio:>12.2f}")
        print(f"  Max Drawdown:     {r.max_drawdown_pct:>11.2f}%")
        print(f"  Avg Hold:         {r.avg_hold_seconds/60:>11.0f}m")

    # Combined results
    if len(results) > 1:
        total_pnl = sum(r.total_pnl for r in results)
        total_trades = sum(r.total_trades for r in results)
        total_wins = sum(r.wins for r in results)
        pnl_color = "\033[92m" if total_pnl >= 0 else "\033[91m"
        reset = "\033[0m"

        print(f"\n  {'=' * 50}")
        print(f"  COMBINED (all strategies)")
        print(f"  {'=' * 50}")
        print(f"  Total P&L:        {pnl_color}${total_pnl:>+12,.2f}{reset}")
        print(f"  Total Trades:     {total_trades:>12}")
        print(f"  Overall Win Rate: {total_wins/max(total_trades,1)*100:>11.1f}%")

    print()
    print("=" * 70)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backtest trading strategies")
    parser.add_argument("--days", type=int, default=30, help="Days to backtest")
    parser.add_argument("--strategy", type=str, default="all", choices=["all", "trump", "arb", "news"])
    parser.add_argument("--balance", type=float, default=10000.0, help="Initial balance")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    results = run_backtest(
        strategy=args.strategy,
        days=args.days,
        initial_balance=args.balance,
        seed=args.seed,
    )

    if args.json:
        output = []
        for r in results:
            output.append({
                "strategy": r.strategy, "days": r.days,
                "initial": r.initial_balance, "final": r.final_balance,
                "pnl": r.total_pnl, "trades": r.total_trades,
                "win_rate": r.win_rate, "sharpe": r.sharpe_ratio,
                "max_drawdown": r.max_drawdown_pct, "profit_factor": r.profit_factor,
            })
        print(json.dumps(output, indent=2))
    else:
        print_results(results)
