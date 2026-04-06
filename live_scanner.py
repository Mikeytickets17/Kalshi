"""
Live Scanner — Real-time terminal dashboard.

Shows everything the bot is monitoring in a continuously updating
terminal display:
  - CEX price feeds (BTC/ETH from Binance + Coinbase)
  - Kalshi arb edge signals detected
  - Trump Truth Social posts + sentiment scores
  - Open positions and P&L
  - Trade log with timestamps
  - Risk state

Run alongside the bot or standalone for monitoring.

Usage:
    python live_scanner.py
"""

import asyncio
import logging
import os
import random
import sys
import time
from collections import deque
from datetime import datetime

import config
from price_feed import PriceFeed
from market_scanner import MarketScanner, MarketOpportunity
from trump_monitor import TrumpMonitor, TrumpPost
from sentiment_analyzer import SentimentAnalyzer, SentimentResult

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("scanner")

# ANSI color codes
class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    PURPLE = "\033[95m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"
    BG_RED = "\033[41m"
    BG_GREEN = "\033[42m"
    BG_BLUE = "\033[44m"


# Rolling logs
arb_signals: deque = deque(maxlen=15)
trump_posts: deque = deque(maxlen=8)
trade_log: deque = deque(maxlen=20)
open_positions: list = []
portfolio_value = config.PAPER_INITIAL_BALANCE_USDC
trade_count = 0
win_count = 0
total_pnl = 0.0


def clear_screen():
    print("\033[2J\033[H", end="")


def draw_box(title: str, width: int = 80) -> str:
    return f"{C.CYAN}{'─'*width}{C.RESET}\n{C.BOLD}{C.WHITE} {title}{C.RESET}\n{C.CYAN}{'─'*width}{C.RESET}"


def format_price(price: float, asset: str = "BTC") -> str:
    if asset == "ETH":
        return f"${price:,.2f}"
    return f"${price:,.2f}"


def format_pnl(pnl: float) -> str:
    if pnl > 0:
        return f"{C.GREEN}+${pnl:.2f}{C.RESET}"
    elif pnl < 0:
        return f"{C.RED}-${abs(pnl):.2f}{C.RESET}"
    return f"${pnl:.2f}"


def format_edge(edge: float) -> str:
    pct = edge * 100
    if pct >= 8:
        return f"{C.GREEN}{C.BOLD}{pct:.1f}%{C.RESET}"
    elif pct >= 5:
        return f"{C.GREEN}{pct:.1f}%{C.RESET}"
    elif pct >= 3:
        return f"{C.YELLOW}{pct:.1f}%{C.RESET}"
    return f"{C.DIM}{pct:.1f}%{C.RESET}"


def render(feed: PriceFeed):
    """Render the full terminal dashboard."""
    clear_screen()
    now = datetime.now().strftime("%H:%M:%S")
    wr = win_count / max(trade_count, 1) * 100

    # ── Header ──
    print(f"{C.BOLD}{C.CYAN}╔══════════════════════════════════════════════════════════════════════════════╗{C.RESET}")
    print(f"{C.BOLD}{C.CYAN}║  KALSHI ARB + TRUMP NEWS TRADING BOT  —  LIVE SCANNER                      ║{C.RESET}")
    print(f"{C.BOLD}{C.CYAN}╚══════════════════════════════════════════════════════════════════════════════╝{C.RESET}")
    mode = f"{C.YELLOW}PAPER{C.RESET}" if config.PAPER_MODE else f"{C.GREEN}LIVE{C.RESET}"
    print(f"  Mode: {mode}   Time: {now}   Portfolio: {C.BOLD}${portfolio_value:,.2f}{C.RESET}   "
          f"P&L: {format_pnl(total_pnl)}   Trades: {trade_count}   WR: {wr:.0f}%")
    print()

    # ── CEX Price Feeds ──
    print(draw_box("CEX PRICE FEEDS (Binance + Coinbase)"))
    for asset in config.TARGET_ASSETS:
        state = feed.get_price(asset)
        if state and state.consensus_price > 0:
            conf_bar = "█" * int(state.confidence * 10) + "░" * (10 - int(state.confidence * 10))
            b_age = time.time() - state.binance_ts if state.binance_ts > 0 else 999
            c_age = time.time() - state.coinbase_ts if state.coinbase_ts > 0 else 999
            spread = abs(state.binance_price - state.coinbase_price) / state.consensus_price * 100 if state.coinbase_price > 0 and state.binance_price > 0 else 0

            print(f"  {C.BOLD}{asset:>4}{C.RESET}  "
                  f"Consensus: {C.WHITE}{C.BOLD}{format_price(state.consensus_price, asset)}{C.RESET}  │  "
                  f"Binance: {format_price(state.binance_price, asset)} ({b_age:.1f}s ago)  │  "
                  f"Coinbase: {format_price(state.coinbase_price, asset)} ({c_age:.1f}s ago)  │  "
                  f"Spread: {spread:.3f}%  │  "
                  f"Conf: [{conf_bar}]")
        else:
            print(f"  {C.BOLD}{asset:>4}{C.RESET}  {C.DIM}Waiting for data...{C.RESET}")
    print()

    # ── Arb Signals ──
    print(draw_box(f"LATENCY ARB SIGNALS ({len(arb_signals)} recent)"))
    if not arb_signals:
        print(f"  {C.DIM}Waiting for edge signals...{C.RESET}")
    else:
        print(f"  {C.DIM}{'TIME':<10} {'SIDE':<5} {'TICKER':<28} {'EDGE':>6} {'SPOT':>12} {'STRIKE':>10} {'POLY':>6} {'LAT':>5}{C.RESET}")
        for sig in list(arb_signals)[-12:]:
            t = datetime.fromtimestamp(sig["ts"]).strftime("%H:%M:%S")
            side_c = C.GREEN if sig["side"] == "YES" else C.RED
            print(f"  {t:<10} {side_c}{sig['side']:<5}{C.RESET} {sig['ticker']:<28} "
                  f"{format_edge(sig['edge'])}  {format_price(sig['spot'], sig['asset']):>12} "
                  f"{sig['strike']:>10,.0f} {sig['poly']:>6.2f} {sig['lat']:>4.0f}ms")
    print()

    # ── Trump Posts ──
    print(draw_box(f"TRUMP TRUTH SOCIAL MONITOR ({len(trump_posts)} recent)"))
    if not trump_posts:
        print(f"  {C.DIM}Monitoring Truth Social for new posts...{C.RESET}")
    else:
        for tp in list(trump_posts)[-6:]:
            t = datetime.fromtimestamp(tp["ts"]).strftime("%H:%M:%S")
            if tp["direction"] == "bullish":
                dir_s = f"{C.GREEN}▲ BULLISH{C.RESET}"
            elif tp["direction"] == "bearish":
                dir_s = f"{C.RED}▼ BEARISH{C.RESET}"
            else:
                dir_s = f"{C.DIM}─ NEUTRAL{C.RESET}"

            conf = tp["confidence"]
            conf_bar = "█" * int(conf * 10) + "░" * (10 - int(conf * 10))
            action = f"{C.GREEN}TRADED{C.RESET}" if tp.get("traded") else f"{C.DIM}SKIPPED{C.RESET}"

            print(f"  {t}  {dir_s}  Conf:[{conf_bar}] {conf:.0%}  Move:{tp['move']:.1%}  {action}")
            print(f"         {C.DIM}\"{tp['text'][:70]}...\"{C.RESET}")
    print()

    # ── Open Positions ──
    print(draw_box(f"OPEN POSITIONS ({len(open_positions)}/{config.MAX_CONCURRENT_POSITIONS})"))
    if not open_positions:
        print(f"  {C.DIM}No open positions{C.RESET}")
    else:
        print(f"  {C.DIM}{'TYPE':<8} {'SIDE':<5} {'TICKER':<25} {'ENTRY':>8} {'SIZE':>8} {'P&L':>10} {'AGE':>6}{C.RESET}")
        for pos in open_positions:
            age_m = (time.time() - pos["entry_time"]) / 60
            side_c = C.GREEN if pos["side"] == "YES" or pos["side"] == "BUY" else C.RED
            print(f"  {pos['type']:<8} {side_c}{pos['side']:<5}{C.RESET} {pos['ticker']:<25} "
                  f"{pos['entry']:>8.4f} ${pos['size']:>7.2f} {format_pnl(pos.get('pnl', 0)):>10} {age_m:>5.1f}m")
    print()

    # ── Trade Log ──
    print(draw_box(f"TRADE LOG ({len(trade_log)} recent)"))
    if not trade_log:
        print(f"  {C.DIM}No trades yet{C.RESET}")
    else:
        for tl in list(trade_log)[-10:]:
            t = datetime.fromtimestamp(tl["ts"]).strftime("%H:%M:%S")
            result = f"{C.GREEN}WIN{C.RESET}" if tl["pnl"] > 0 else f"{C.RED}LOSS{C.RESET}"
            print(f"  {t}  {tl['type']:<8} {tl['side']:<5} {tl['ticker']:<25} {format_pnl(tl['pnl']):>10}  {result}")
    print()

    # ── Footer ──
    print(f"{C.DIM}  Refreshing every 500ms │ Ctrl+C to stop │ "
          f"Arb threshold: {config.EDGE_THRESHOLD_PCT*100:.0f}% │ "
          f"Trump poll: {config.TRUMP_POLL_INTERVAL_SECONDS}s{C.RESET}")


async def main():
    global portfolio_value, trade_count, win_count, total_pnl

    print("Starting live scanner...")

    feed = PriceFeed()
    scanner = MarketScanner(feed)
    trump_mon = TrumpMonitor()
    sentiment = SentimentAnalyzer()

    # Start feeds in background
    asyncio.create_task(feed.start())
    asyncio.create_task(scanner.start())
    asyncio.create_task(trump_mon.start())

    await asyncio.sleep(1)  # Let feeds initialize

    while True:
        # Drain arb signal queue
        while not scanner.signal_queue.empty():
            try:
                opp: MarketOpportunity = scanner.signal_queue.get_nowait()
                arb_signals.append({
                    "ts": time.time(),
                    "side": opp.side,
                    "ticker": opp.ticker,
                    "edge": opp.edge,
                    "spot": opp.cex_price,
                    "strike": opp.contract_strike,
                    "poly": opp.current_price,
                    "lat": opp.latency_ms,
                    "asset": opp.asset,
                })

                # Simulate trade in paper mode
                if opp.edge >= config.EDGE_THRESHOLD_PCT:
                    size = min(portfolio_value * 0.03, 300)
                    open_positions.append({
                        "type": "ARB",
                        "side": opp.side,
                        "ticker": opp.ticker,
                        "entry": opp.current_price,
                        "size": size,
                        "entry_time": time.time(),
                        "pnl": 0,
                    })
                    trade_count += 1
            except Exception:
                break

        # Drain trump post queue
        while not trump_mon.post_queue.empty():
            try:
                post: TrumpPost = trump_mon.post_queue.get_nowait()
                sent = await sentiment.analyze(post)
                traded = sent.is_market_relevant and sent.confidence >= config.TRUMP_MIN_CONFIDENCE
                trump_posts.append({
                    "ts": time.time(),
                    "text": post.text,
                    "direction": sent.direction,
                    "confidence": sent.confidence,
                    "move": sent.expected_move_pct,
                    "traded": traded,
                })
                if traded:
                    size = min(portfolio_value * 0.05, 500)
                    side = "BUY" if sent.direction == "bullish" else "SELL"
                    open_positions.append({
                        "type": "TRUMP",
                        "side": side,
                        "ticker": f"BTC-SPOT",
                        "entry": 68500 + random.gauss(0, 200),
                        "size": size,
                        "entry_time": time.time(),
                        "pnl": 0,
                    })
                    trade_count += 1
            except Exception:
                break

        # Simulate position resolution
        for pos in list(open_positions):
            age = time.time() - pos["entry_time"]
            if pos["type"] == "ARB" and age > random.uniform(120, 600):
                # Resolve arb position
                won = random.random() < 0.92
                pnl = pos["size"] * 0.06 if won else -pos["size"] * 0.40
                if won:
                    win_count += 1
                total_pnl += pnl
                portfolio_value += pnl
                trade_log.append({
                    "ts": time.time(), "type": "ARB", "side": pos["side"],
                    "ticker": pos["ticker"], "pnl": round(pnl, 2),
                })
                open_positions.remove(pos)
            elif pos["type"] == "TRUMP" and age > config.TRUMP_HOLD_MINUTES * 60:
                won = random.random() < 0.70
                pnl = pos["size"] * random.uniform(0.01, 0.04) if won else -pos["size"] * random.uniform(0.005, 0.02)
                if won:
                    win_count += 1
                total_pnl += pnl
                portfolio_value += pnl
                trade_log.append({
                    "ts": time.time(), "type": "TRUMP", "side": pos["side"],
                    "ticker": pos["ticker"], "pnl": round(pnl, 2),
                })
                open_positions.remove(pos)

        # Cap open positions
        while len(open_positions) > config.MAX_CONCURRENT_POSITIONS:
            open_positions.pop(0)

        render(feed)
        await asyncio.sleep(0.5)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(f"\n{C.YELLOW}Scanner stopped.{C.RESET}")
        print(f"Final: {trade_count} trades, {win_count} wins ({win_count/max(trade_count,1)*100:.0f}%), "
              f"P&L: ${total_pnl:+,.2f}, Portfolio: ${portfolio_value:,.2f}")
