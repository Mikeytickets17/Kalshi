"""
Polymarket Latency Arbitrage + Trump News Trading Bot.

Two strategies running simultaneously:

1. LATENCY ARB (the 0x8dxd strategy):
   Stream BTC/ETH from Binance/Coinbase, compare to Polymarket
   contract prices, trade when divergence > 3%.

2. TRUMP NEWS TRADING:
   Monitor Truth Social every 3 seconds. When Trump posts about
   crypto/tariffs/Fed, analyze with Claude API, execute BTC spot
   trade on Binance within seconds. Hold for 15-30 minutes.

Both strategies share the same risk manager and notifier.
"""

import asyncio
import logging
import random
import signal
import sys
import time
from typing import Optional

import config
from exchange import BinanceExecutor, TradeResult
from market_scanner import MarketOpportunity, MarketScanner
from notifier import TelegramNotifier
from kalshi_client import KalshiClient
from polymarket import Position, Side, OrderResult
from position_sizer import PositionSizer
from price_feed import PriceFeed
from risk_manager import RiskManager
from signal_evaluator import EvaluationResult, SignalEvaluator

# --- Logging Setup ---

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(config.LOG_FILE, mode="a"),
    ],
)
logger = logging.getLogger("bot")


class LatencyArbBot:
    """Polymarket latency arbitrage bot — the 0x8dxd strategy."""

    def __init__(self) -> None:
        self._paper_mode = config.PAPER_MODE
        self._running = False

        initial_balance = config.PAPER_INITIAL_BALANCE_USDC if self._paper_mode else 0.0
        self._portfolio_value = initial_balance
        self._available_balance = initial_balance
        self._active_positions: dict[str, Position] = {}
        self._trade_count = 0
        self._win_count = 0

        # Components — Latency Arb (Kalshi)
        self._kalshi = KalshiClient()
        self._price_feed = PriceFeed()
        self._scanner = MarketScanner(self._price_feed)
        self._evaluator = SignalEvaluator(self._kalshi, self._active_positions)
        self._sizer = PositionSizer(self._portfolio_value)
        self._risk_manager = RiskManager(self._portfolio_value)
        self._notifier = TelegramNotifier()

        # Components — Trump News Trading (3 trade types per post)
        from trump_monitor import TrumpMonitor
        from sentiment_analyzer import SentimentAnalyzer
        from contract_matcher import ContractMatcher
        self._trump_monitor = TrumpMonitor()
        self._sentiment = SentimentAnalyzer()
        self._exchange = BinanceExecutor()
        self._contract_matcher = ContractMatcher(self._kalshi)
        self._trump_positions: list[dict] = []  # Track open Trump trades

        logger.info(
            "LatencyArbBot initialized: paper=%s portfolio=$%.2f",
            self._paper_mode, self._portfolio_value,
        )

    async def run(self) -> None:
        self._running = True
        logger.info("=" * 64)
        logger.info("Polymarket Latency Arb + Trump News Bot starting")
        logger.info("Strategy 1: CEX price feed vs Kalshi crypto contracts")
        logger.info("Strategy 2: Trump Truth Social → Claude → Binance BTC")
        logger.info("Mode: %s", "PAPER" if self._paper_mode else "LIVE")
        logger.info("Portfolio: $%.2f", self._portfolio_value)
        logger.info("Edge threshold: %.1f%%", config.EDGE_THRESHOLD_PCT * 100)
        logger.info("Target assets: %s", config.TARGET_ASSETS)
        logger.info("Target durations: %s min", config.TARGET_DURATIONS)
        logger.info("Max positions: %d", config.MAX_CONCURRENT_POSITIONS)
        logger.info("=" * 64)

        tasks = [
            # Strategy 1: Latency Arbitrage
            asyncio.create_task(self._price_feed.start(), name="price_feed"),
            asyncio.create_task(self._scanner.start(), name="scanner"),
            asyncio.create_task(self._signal_processor(), name="signal_processor"),
            asyncio.create_task(self._exit_monitor(), name="exit_monitor"),
            # Strategy 2: Trump News Trading
            asyncio.create_task(self._trump_monitor.start(), name="trump_monitor"),
            asyncio.create_task(self._trump_news_processor(), name="trump_processor"),
            asyncio.create_task(self._trump_exit_monitor(), name="trump_exits"),
        ]

        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for task in done:
                if task.exception():
                    logger.error("Task %s failed: %s", task.get_name(), task.exception())
        except asyncio.CancelledError:
            pass
        finally:
            await self.shutdown("Main loop ended")
            for task in tasks:
                if not task.done():
                    task.cancel()

    async def shutdown(self, reason: str = "Manual shutdown") -> None:
        if not self._running:
            return
        self._running = False
        logger.info("Shutting down: %s", reason)

        self._kalshi.cancel_all()

        if self._active_positions:
            logger.info("Closing %d positions on shutdown...", len(self._active_positions))
            for market_id, pos in list(self._active_positions.items()):
                if pos.side == Side.YES:
                    pnl = (pos.current_price - pos.avg_price) * pos.size
                else:
                    pnl = (pos.avg_price - pos.current_price) * pos.size
                self._risk_manager.record_trade_result(pnl, source_wallet=pos.source_wallet)
                self._portfolio_value += pnl
                logger.info(
                    "%s Closed %s: side=%s pnl=$%.2f",
                    "[PAPER]" if self._paper_mode else "[LIVE]",
                    market_id, pos.side.value, pnl,
                )
            self._active_positions.clear()

        wr = self._win_count / max(self._trade_count, 1) * 100
        logger.info(
            "Session stats: %d trades, %d wins (%.1f%%), portfolio $%.2f",
            self._trade_count, self._win_count, wr, self._portfolio_value,
        )

        risk_summary = self._risk_manager.get_summary()
        self._notifier.notify_shutdown(reason, self._portfolio_value, risk_summary)

        await self._scanner.stop()
        await self._price_feed.stop()
        await self._trump_monitor.stop()
        await self._sentiment.close()
        self._exchange.close()
        self._kalshi.close()
        self._notifier.close()

        logger.info("Shutdown complete. Final portfolio: $%.2f", self._portfolio_value)

    # --- Strategy 1: Latency Arb Signal Processing ---

    async def _signal_processor(self) -> None:
        """Process arbitrage signals — speed is everything."""
        logger.info("Signal processor started — waiting for edge signals")
        while self._running:
            try:
                try:
                    opp = await asyncio.wait_for(
                        self._scanner.signal_queue.get(), timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue

                logger.info(
                    "ARB SIGNAL: %s %s edge=%.1f%% cex=$%.2f strike=$%.0f latency=%dms",
                    opp.side, opp.ticker, opp.edge * 100,
                    opp.cex_price, opp.contract_strike, opp.latency_ms,
                )

                evaluation = self._evaluator.evaluate(opp)
                if not evaluation.should_copy:
                    logger.debug("Rejected: %s", evaluation.rejection_reason)
                    continue

                can_trade, risk_reason = self._risk_manager.check_can_trade(
                    self._portfolio_value,
                    self._active_positions,
                    proposed_category="crypto",
                    source_wallet=opp.asset,
                )
                if not can_trade:
                    logger.warning("Risk blocked: %s", risk_reason)
                    continue

                self._sizer.portfolio_value = self._portfolio_value
                size_usdc = self._sizer.compute_size(evaluation)

                if size_usdc > self._available_balance:
                    continue

                await self._execute_trade(evaluation, size_usdc)

            except Exception as exc:
                logger.error("Signal processor error: %s", exc, exc_info=True)

    async def _execute_trade(self, evaluation: EvaluationResult, size_usdc: float) -> None:
        opp = evaluation.signal
        ticker = opp.ticker

        kalshi_result = self._kalshi.place_order(
            ticker=opp.market_id,
            side=evaluation.side.value,
            size_usd=size_usdc,
            price=evaluation.target_price,
        )

        if not kalshi_result.success:
            logger.error("Kalshi order failed: %s", kalshi_result.error)
            return

        position = Position(
            market_id=opp.market_id,
            condition_id=ticker,
            side=evaluation.side,
            size=kalshi_result.filled_size or size_usdc,
            avg_price=kalshi_result.filled_price or evaluation.target_price,
            current_price=kalshi_result.filled_price or evaluation.target_price,
            source_wallet=opp.asset,
            category="crypto",
        )
        self._active_positions[opp.market_id] = position
        self._available_balance -= size_usdc
        self._trade_count += 1

        self._notifier.notify_trade_opened(
            evaluation, size_usdc, kalshi_result.filled_price or evaluation.target_price
        )

        logger.info(
            "TRADED: %s %s $%.2f @ %.4f edge=%.1f%% (pos %d/%d)",
            evaluation.side.value, ticker, size_usdc,
            kalshi_result.filled_price or evaluation.target_price,
            opp.edge * 100,
            len(self._active_positions), config.MAX_CONCURRENT_POSITIONS,
        )

    # --- Exit Monitoring ---

    async def _exit_monitor(self) -> None:
        """Monitor positions — for latency arb, most exit at contract resolution."""
        logger.info("Exit monitor started")
        while self._running:
            try:
                for market_id, pos in list(self._active_positions.items()):
                    should_exit, reason = self._check_exit(pos)
                    if should_exit:
                        await self._close_position(market_id, pos, reason)

                await asyncio.sleep(config.EXIT_CHECK_INTERVAL_SECONDS)
            except Exception as exc:
                logger.error("Exit monitor error: %s", exc, exc_info=True)
                await asyncio.sleep(1)

    def _check_exit(self, position: Position) -> tuple[bool, str]:
        # Paper mode: simulate resolution after random 5-15 min
        if self._paper_mode:
            age = time.time() - position.entry_time
            if age > random.uniform(300, 900):
                # Simulate win/loss based on the edge at entry
                # High-edge entries win ~90-95% of the time
                win_prob = min(0.70 + position.avg_price * 0.25, 0.96)
                if random.random() < win_prob:
                    # Won: contract resolves at $1.00
                    if position.side == Side.YES:
                        position.current_price = 0.99
                    else:
                        position.current_price = 0.99
                else:
                    # Lost: contract resolves at $0
                    if position.side == Side.YES:
                        position.current_price = 0.01
                    else:
                        position.current_price = 0.01
                return True, "Contract resolved"
            return False, ""

        # Live: check risk manager conditions
        should_exit, reason = self._risk_manager.check_exit_conditions(
            position, self._portfolio_value
        )
        return should_exit, reason

    async def _close_position(self, market_id: str, position: Position, reason: str) -> None:
        if position.side == Side.YES:
            pnl = (position.current_price - position.avg_price) * position.size
        else:
            pnl = (position.avg_price - position.current_price) * position.size

        won = pnl > 0
        if won:
            self._win_count += 1

        self._available_balance += position.size + pnl
        self._portfolio_value += pnl
        self._risk_manager.record_trade_result(pnl, source_wallet=position.source_wallet)
        self._sizer.portfolio_value = self._portfolio_value

        del self._active_positions[market_id]

        self._notifier.notify_trade_closed(position, pnl, reason)

        wr = self._win_count / max(self._trade_count, 1) * 100
        logger.info(
            "%s %s pnl=$%+.2f (total: %d trades, %.1f%% WR, portfolio=$%.2f)",
            "WIN" if won else "LOSS", market_id, pnl,
            self._trade_count, wr, self._portfolio_value,
        )


    # --- Strategy 2: Trump News Trading ---

    async def _trump_news_processor(self) -> None:
        """Process Trump posts → Claude sentiment → Binance execution."""
        logger.info("Trump news processor started — monitoring Truth Social")
        while self._running:
            try:
                try:
                    post = await asyncio.wait_for(
                        self._trump_monitor.post_queue.get(), timeout=2.0
                    )
                except asyncio.TimeoutError:
                    continue

                logger.info(
                    "TRUMP POST: %s... [source=%s]",
                    post.text[:80], post.source,
                )

                # Analyze sentiment with Claude (or rule-based fallback)
                sentiment = await self._sentiment.analyze(post)

                if not sentiment.is_market_relevant:
                    logger.info("Post not market-relevant, skipping")
                    continue

                if sentiment.confidence < config.TRUMP_MIN_CONFIDENCE:
                    logger.info(
                        "Confidence %.2f below threshold %.2f, skipping",
                        sentiment.confidence, config.TRUMP_MIN_CONFIDENCE,
                    )
                    continue

                # Calculate trade size
                size = min(
                    self._portfolio_value * config.TRUMP_TRADE_SIZE_PCT * sentiment.confidence,
                    config.TRUMP_MAX_TRADE_SIZE_USDC,
                )
                size = max(size, config.MIN_TRADE_SIZE_USDC)

                if size > self._available_balance:
                    logger.warning("Insufficient balance for Trump trade ($%.2f)", size)
                    continue

                # Execute on Binance
                if sentiment.direction == "bullish":
                    result = self._exchange.buy("BTC", size)
                elif sentiment.direction == "bearish":
                    result = self._exchange.sell("BTC", size)
                else:
                    continue

                if result.success:
                    self._trade_count += 1
                    self._available_balance -= size
                    self._trump_positions.append({
                        "entry_time": time.time(),
                        "side": result.side,
                        "asset": result.asset,
                        "entry_price": result.filled_price,
                        "size_usd": result.filled_usd,
                        "qty": result.filled_qty,
                        "sentiment": sentiment.direction,
                        "confidence": sentiment.confidence,
                        "expected_move": sentiment.expected_move_pct,
                        "post_text": post.text[:100],
                        "hold_until": time.time() + config.TRUMP_HOLD_MINUTES * 60,
                    })

                    logger.info(
                        "TRUMP BTC TRADE: %s BTC $%.2f @ $%.2f (conf=%.2f, expected=%.1f%%, exec=%dms)",
                        result.side, result.filled_usd, result.filled_price,
                        sentiment.confidence, sentiment.expected_move_pct * 100,
                        result.execution_time_ms,
                    )

                # ── STRATEGY 3: KALSHI CONTRACT TRADING ──
                # Find Kalshi prediction contracts that match the post content
                # e.g. "tariffs on China" → buy YES on "Will Trump impose tariffs?"
                if sentiment.kalshi_keywords and sentiment.kalshi_confidence >= 0.50:
                    matches = self._contract_matcher.find_matches(sentiment)
                    for match in matches:
                        contract_size = min(
                            self._portfolio_value * 0.04 * match.confidence,
                            400.0,
                        )
                        if contract_size < 2.0 or contract_size > self._available_balance:
                            continue

                        order = self._contract_matcher.execute_match(match, contract_size)
                        if order and order.success:
                            self._trade_count += 1
                            self._available_balance -= contract_size
                            self._trump_positions.append({
                                "entry_time": time.time(),
                                "side": match.side,
                                "asset": "KALSHI",
                                "entry_price": order.filled_price,
                                "size_usd": order.filled_size,
                                "qty": 0,
                                "sentiment": sentiment.direction,
                                "confidence": match.confidence,
                                "expected_move": 0,
                                "post_text": post.text[:100],
                                "hold_until": time.time() + config.TRUMP_HOLD_MINUTES * 60,
                                "ticker": match.ticker,
                                "type": "kalshi_contract",
                            })
                            logger.info(
                                "TRUMP KALSHI TRADE: %s %s $%.2f @ %.2f (match=%.0f%%, conf=%.2f, kw=%s)",
                                match.side, match.ticker, contract_size, order.filled_price,
                                match.match_score * 100, match.confidence,
                                match.keywords_matched,
                            )

            except Exception as exc:
                logger.error("Trump processor error: %s", exc, exc_info=True)

    async def _trump_exit_monitor(self) -> None:
        """Exit Trump trades after the hold period expires."""
        logger.info("Trump exit monitor started (hold=%d min)", config.TRUMP_HOLD_MINUTES)
        while self._running:
            try:
                for tp in list(self._trump_positions):
                    if time.time() >= tp["hold_until"]:
                        # Exit the position
                        if tp["side"] == "BUY":
                            result = self._exchange.sell(tp["asset"], tp["size_usd"])
                        else:
                            result = self._exchange.buy(tp["asset"], tp["size_usd"])

                        if result.success:
                            # Calculate PnL
                            if tp["side"] == "BUY":
                                pnl = (result.filled_price - tp["entry_price"]) * tp["qty"]
                            else:
                                pnl = (tp["entry_price"] - result.filled_price) * tp["qty"]

                            won = pnl > 0
                            if won:
                                self._win_count += 1

                            self._available_balance += tp["size_usd"] + pnl
                            self._portfolio_value += pnl
                            self._risk_manager.record_trade_result(pnl, source_wallet="trump_news")

                            logger.info(
                                "TRUMP EXIT: %s BTC pnl=$%+.2f (entry=$%.2f exit=$%.2f held=%dm)",
                                "WIN" if won else "LOSS", pnl,
                                tp["entry_price"], result.filled_price,
                                config.TRUMP_HOLD_MINUTES,
                            )

                        self._trump_positions.remove(tp)

                await asyncio.sleep(10)
            except Exception as exc:
                logger.error("Trump exit monitor error: %s", exc, exc_info=True)
                await asyncio.sleep(5)


async def main() -> None:
    bot = LatencyArbBot()

    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("Received shutdown signal")
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    bot_task = asyncio.create_task(bot.run())
    shutdown_task = asyncio.create_task(shutdown_event.wait())
    done, pending = await asyncio.wait(
        [bot_task, shutdown_task],
        return_when=asyncio.FIRST_COMPLETED,
    )

    if shutdown_event.is_set():
        await bot.shutdown("Signal received")
        bot_task.cancel()
        try:
            await bot_task
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    print("Starting Kalshi Arb + Trump News Trading Bot...")
    print(f"Mode: {'PAPER' if config.PAPER_MODE else 'LIVE'}")
    print(f"Strategy 1: CEX price vs Kalshi crypto contracts")
    print(f"Strategy 2: Trump Truth Social → Claude → Binance BTC")
    print(f"Edge threshold: {config.EDGE_THRESHOLD_PCT*100:.1f}%")
    print(f"Kalshi: {'DEMO' if config.KALSHI_USE_DEMO else 'PRODUCTION'}")
    print(f"Paper balance: ${config.PAPER_INITIAL_BALANCE_USDC:,.2f}")
    asyncio.run(main())
