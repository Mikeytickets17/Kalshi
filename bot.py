"""
Multi-Strategy Trading Bot — Kalshi + Binance + Stocks.

4 strategies running simultaneously, all legal from NJ:

1. LATENCY ARB: CEX price vs Kalshi crypto contracts
2. TRUMP NEWS: Truth Social → Claude → BTC spot + Kalshi contracts
3. BREAKING NEWS: Reuters/AP/Fed/BLS → Claude → stocks + BTC + Kalshi
4. KALSHI CONTRACTS: Match any news to Kalshi prediction markets

Venues: Kalshi (contracts), Binance (BTC/ETH spot+futures), Alpaca (US stocks)
"""

import asyncio
import logging
import random
import signal
import sys
import time
from typing import Optional

import config
import shared_state
from exchange import BinanceExecutor, TradeResult
from kalshi_client import KalshiClient
from market_scanner import MarketOpportunity, MarketScanner
from news_analyzer import NewsAnalyzer, TradeAction
from news_feed import NewsFeed, NewsItem
from notifier import TelegramNotifier
from orderbook import OrderBookReader
from polymarket import Position, Side, OrderResult
from position_sizer import PositionSizer
from price_feed import PriceFeed
from risk_manager import RiskManager
from signal_evaluator import EvaluationResult, SignalEvaluator
from stock_trader import StockTrader

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

        # Components — Trump News Trading
        from trump_monitor import TrumpMonitor
        from sentiment_analyzer import SentimentAnalyzer
        from contract_matcher import ContractMatcher
        self._trump_monitor = TrumpMonitor()
        self._sentiment = SentimentAnalyzer()
        self._exchange = BinanceExecutor()
        self._contract_matcher = ContractMatcher(self._kalshi)
        self._trump_positions: list[dict] = []

        # Components — Order Book + Flow Analysis
        self._orderbook = OrderBookReader()

        # Components — Universal News Trading (stocks, BTC, Kalshi)
        self._news_feed = NewsFeed()
        self._news_analyzer = NewsAnalyzer()
        self._stock_trader = StockTrader()
        self._news_positions: list[dict] = []

        # Initialize shared state for the dashboard
        shared_state.init(self._portfolio_value)

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

        # Send startup notification
        self._notifier.notify_startup(
            self._portfolio_value,
            "PAPER" if self._paper_mode else "LIVE",
        )

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
            # Order Book + Flow Analysis
            asyncio.create_task(self._orderbook.start(), name="orderbook"),
            # Strategy 3: Universal News → Stocks + BTC + Kalshi
            asyncio.create_task(self._news_feed.start(), name="news_feed"),
            asyncio.create_task(self._news_processor(), name="news_processor"),
            asyncio.create_task(self._news_exit_monitor(), name="news_exits"),
            # State persistence for dashboard
            asyncio.create_task(self._state_flusher(), name="state_flush"),
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

    async def _state_flusher(self) -> None:
        """Periodically flush shared state to disk for the dashboard."""
        while self._running:
            try:
                shared_state.periodic_flush()
            except Exception:
                pass
            await asyncio.sleep(3)

    async def shutdown(self, reason: str = "Manual shutdown") -> None:
        if not self._running:
            return
        self._running = False
        shared_state.set_bot_running(False)
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
        await self._orderbook.stop()
        await self._trump_monitor.stop()
        await self._news_feed.stop()
        await self._sentiment.close()
        await self._news_analyzer.close()
        self._exchange.close()
        self._stock_trader.close()
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

        # Record to shared state for dashboard
        shared_state.record_trade_opened(
            trade_id=opp.market_id,
            strategy="ARB",
            side=evaluation.side.value,
            asset=opp.asset,
            venue="Kalshi",
            entry_price=kalshi_result.filled_price or evaluation.target_price,
            size_usd=size_usdc,
            confidence=opp.edge,
            reason=f"CEX ${opp.cex_price:.0f} vs strike ${opp.contract_strike:.0f} ({opp.edge*100:.1f}% edge)",
        )
        shared_state.record_signal(
            strategy="ARB", side=evaluation.side.value, asset=opp.asset,
            venue="Kalshi", confidence=opp.edge,
            reason=f"Edge {opp.edge*100:.1f}% latency={opp.latency_ms:.0f}ms",
            action="TRADED",
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
                # Simulate win/loss: lower entry price = more edge = higher win rate
                # Buying YES at 0.40 has more upside than at 0.90
                edge_factor = 1.0 - position.avg_price  # higher when entry is lower
                win_prob = min(0.60 + edge_factor * 0.35, 0.95)
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

        # Record to shared state for dashboard
        shared_state.record_trade_closed(
            trade_id=market_id,
            pnl=pnl,
            exit_price=position.current_price,
            reason=reason,
        )
        shared_state.update_portfolio(self._portfolio_value)

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

                # Record trump post to shared state for dashboard
                shared_state.record_trump_post(
                    text=post.text, source=post.source,
                )

                # Analyze sentiment with Claude (or rule-based fallback)
                sentiment = await self._sentiment.analyze(post)

                # Alert: Trump post detected with sentiment
                self._notifier.notify_trump_post_detected(
                    post_text=post.text,
                    source=post.source,
                    sentiment=sentiment.direction,
                    confidence=sentiment.confidence,
                    latency_ms=post.detection_latency_ms,
                )

                if not sentiment.is_market_relevant:
                    logger.info("Post not market-relevant, skipping")
                    continue

                if sentiment.confidence < config.TRUMP_MIN_CONFIDENCE:
                    logger.info(
                        "Confidence %.2f below threshold %.2f, skipping",
                        sentiment.confidence, config.TRUMP_MIN_CONFIDENCE,
                    )
                    continue

                # WAIT for market reaction before committing
                logger.info("Trump post detected — waiting 2s for order flow confirmation...")
                await asyncio.sleep(2)

                # CHECK ORDER BOOK: is the market actually reacting?
                btc_decision = self._orderbook.make_decision(
                    "BTC", sentiment.direction, sentiment.confidence
                )

                if not btc_decision.should_trade:
                    logger.info(
                        "ORDER BOOK REJECTED Trump trade: %s",
                        btc_decision.reason,
                    )
                    # Still try Kalshi contracts even if BTC flow doesn't confirm
                    # (contracts react differently than spot)
                else:
                    logger.info(
                        "ORDER BOOK CONFIRMED: %s BTC — %s",
                        btc_decision.side, btc_decision.reason,
                    )

                # Calculate trade size — use book-confirmed direction
                trade_side = btc_decision.side if btc_decision.should_trade else None
                size = min(
                    self._portfolio_value * config.TRUMP_TRADE_SIZE_PCT * btc_decision.confidence,
                    config.TRUMP_MAX_TRADE_SIZE_USDC,
                )
                size = max(size, config.MIN_TRADE_SIZE_USDC)

                if size > self._available_balance:
                    logger.warning("Insufficient balance for Trump trade ($%.2f)", size)
                    continue

                # Execute on Binance ONLY if order book confirms
                if trade_side == "BUY":
                    result = self._exchange.buy("BTC", size)
                elif trade_side == "SELL":
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

                    trade_id = f"trump-btc-{int(time.time()*1000)}"
                    self._trump_positions[-1]["trade_id"] = trade_id
                    shared_state.record_trade_opened(
                        trade_id=trade_id,
                        strategy="TRUMP",
                        side=result.side,
                        asset="BTC",
                        venue="Binance",
                        entry_price=result.filled_price,
                        size_usd=result.filled_usd,
                        confidence=sentiment.confidence,
                        reason=post.text[:80],
                    )
                    shared_state.record_trump_post(
                        text=post.text, source=post.source,
                        sentiment=sentiment.direction,
                        confidence=sentiment.confidence,
                    )

                    self._notifier.notify_trump_trade(
                        side=result.side, asset="BTC", venue="Binance",
                        size_usd=result.filled_usd, entry_price=result.filled_price,
                        confidence=sentiment.confidence, post_text=post.text,
                    )

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
                            kalshi_trade_id = f"trump-k-{int(time.time()*1000)}"
                            self._trump_positions[-1]["trade_id"] = kalshi_trade_id
                            shared_state.record_trade_opened(
                                trade_id=kalshi_trade_id,
                                strategy="TRUMP",
                                side=match.side,
                                asset=match.ticker,
                                venue="Kalshi",
                                entry_price=order.filled_price,
                                size_usd=order.filled_size,
                                confidence=match.confidence,
                                reason=f"Contract match: {match.keywords_matched}",
                            )

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
                            # Calculate PnL — use qty for spot, size_usd for contracts
                            if tp.get("type") == "kalshi_contract" or tp["qty"] == 0:
                                # Kalshi contract: P&L = (exit - entry) * contracts
                                # size_usd was the cost, exit is the contract resolution
                                if tp["side"] in ("YES", "BUY"):
                                    pnl = tp["size_usd"] * (result.filled_price / max(tp["entry_price"], 0.01) - 1)
                                else:
                                    pnl = tp["size_usd"] * (1 - result.filled_price / max(tp["entry_price"], 0.01))
                            else:
                                # Spot BTC/ETH: P&L = price_change * quantity
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

                            # Record to shared state
                            trade_id = tp.get("trade_id", f"trump-exit-{int(time.time()*1000)}")
                            shared_state.record_trade_closed(
                                trade_id=trade_id,
                                pnl=pnl,
                                exit_price=result.filled_price,
                                reason=f"Hold period expired ({config.TRUMP_HOLD_MINUTES}m)",
                            )
                            shared_state.update_portfolio(self._portfolio_value)

                            logger.info(
                                "TRUMP EXIT: %s BTC pnl=$%+.2f (entry=$%.2f exit=$%.2f held=%dm)",
                                "WIN" if won else "LOSS", pnl,
                                tp["entry_price"], result.filled_price,
                                config.TRUMP_HOLD_MINUTES,
                            )

                        else:
                            # Exit failed — increment retry counter, remove after 3 retries
                            tp["_exit_retries"] = tp.get("_exit_retries", 0) + 1
                            if tp["_exit_retries"] >= 3:
                                logger.warning("TRUMP EXIT FAILED after 3 retries, dropping position: %s", tp.get("asset"))
                                self._trump_positions.remove(tp)
                            continue

                        self._trump_positions.remove(tp)

                await asyncio.sleep(10)
            except Exception as exc:
                logger.error("Trump exit monitor error: %s", exc, exc_info=True)
                await asyncio.sleep(5)


    # --- Strategy 3: Universal News → Multi-Venue Execution ---

    async def _news_processor(self) -> None:
        """Process breaking news → analyze → trade across all venues."""
        logger.info("News processor started — monitoring Reuters, AP, Fed, BLS, CNBC...")
        while self._running:
            try:
                try:
                    news = await asyncio.wait_for(
                        self._news_feed.news_queue.get(), timeout=2.0
                    )
                except asyncio.TimeoutError:
                    continue

                logger.info(
                    "NEWS [%s]: %s [%s]",
                    news.priority.upper(), news.headline[:80], news.source,
                )

                # Analyze with Claude or rules
                actions = await self._news_analyzer.analyze(news)
                if not actions:
                    logger.debug("No tradeable actions from this headline")
                    continue

                # WAIT 2 seconds for market to react before trading
                logger.info("News detected — waiting 2s for market reaction...")
                await asyncio.sleep(2)

                # Execute each action — but ONLY if order book confirms
                for action in actions:
                    if action.confidence < 0.50:
                        continue

                    # CHECK ORDER BOOK + FLOW before every trade
                    if action.venue in ("binance_spot", "binance_futures") and action.asset in ("BTC", "ETH"):
                        decision = self._orderbook.make_decision(
                            action.asset, action.side, action.confidence
                        )
                        if not decision.should_trade:
                            logger.info(
                                "ORDER BOOK REJECTED: %s %s — %s",
                                action.side, action.asset, decision.reason,
                            )
                            continue

                        # Use book-adjusted confidence and size
                        action.confidence = decision.confidence
                        logger.info(
                            "ORDER BOOK CONFIRMED: %s %s — %s (conf=%.2f)",
                            decision.side, action.asset, decision.reason, decision.confidence,
                        )

                    size = min(
                        self._portfolio_value * action.size_pct * action.confidence,
                        config.TRUMP_MAX_TRADE_SIZE_USDC,
                    )
                    if size < 2.0 or size > self._available_balance:
                        continue

                    result = await self._execute_news_action(action, size, news)
                    if result:
                        self._available_balance -= size

            except Exception as exc:
                logger.error("News processor error: %s", exc, exc_info=True)

    async def _execute_news_action(self, action: TradeAction, size: float, news: NewsItem) -> bool:
        """Execute a single trade action on the appropriate venue."""
        try:
            if action.venue == "binance_spot":
                if action.side in ("BUY", "LONG"):
                    result = self._exchange.buy(action.asset, size)
                else:
                    result = self._exchange.sell(action.asset, size)
                if result.success:
                    news_tid = f"news-bin-{int(time.time()*1000)}"
                    self._trade_count += 1
                    self._news_positions.append({
                        "entry_time": time.time(),
                        "venue": "binance",
                        "asset": action.asset,
                        "side": action.side,
                        "size_usd": size,
                        "entry_price": result.filled_price,
                        "hold_until": time.time() + action.hold_minutes * 60,
                        "headline": news.headline[:80],
                        "trade_id": news_tid,
                    })
                    shared_state.record_trade_opened(
                        trade_id=news_tid, strategy="NEWS", side=action.side,
                        asset=action.asset, venue="Binance",
                        entry_price=result.filled_price, size_usd=size,
                        confidence=action.confidence, reason=news.headline[:80],
                    )
                    shared_state.record_news(news.headline, news.source, news.priority, news.category)
                    logger.info(
                        "NEWS TRADE: %s %s $%.2f on Binance (conf=%.2f, reason=%s)",
                        action.side, action.asset, size, action.confidence, action.reasoning[:40],
                    )
                    return True

            elif action.venue == "alpaca_stock":
                if action.side in ("BUY", "LONG"):
                    result = self._stock_trader.buy(action.asset, size)
                else:
                    result = self._stock_trader.sell(action.asset, size)
                if result.success:
                    stock_tid = f"news-alp-{int(time.time()*1000)}"
                    self._trade_count += 1
                    self._news_positions.append({
                        "entry_time": time.time(),
                        "venue": "alpaca",
                        "asset": action.asset,
                        "side": action.side,
                        "size_usd": size,
                        "entry_price": result.filled_price,
                        "hold_until": time.time() + action.hold_minutes * 60,
                        "headline": news.headline[:80],
                        "trade_id": stock_tid,
                    })
                    shared_state.record_trade_opened(
                        trade_id=stock_tid, strategy="NEWS", side=action.side,
                        asset=action.asset, venue="Alpaca",
                        entry_price=result.filled_price, size_usd=size,
                        confidence=action.confidence, reason=news.headline[:80],
                    )
                    shared_state.record_news(news.headline, news.source, news.priority, news.category)
                    logger.info(
                        "STOCK TRADE: %s %s $%.2f on Alpaca (conf=%.2f, reason=%s)",
                        action.side, action.asset, size, action.confidence, action.reasoning[:40],
                    )
                    return True

            elif action.venue == "kalshi_contract" and action.kalshi_keywords:
                from contract_matcher import ContractMatch
                matches = self._contract_matcher.find_matches(
                    type("S", (), {
                        "kalshi_keywords": action.kalshi_keywords,
                        "kalshi_side": action.kalshi_side,
                        "kalshi_confidence": action.confidence,
                    })()
                )
                for match in matches[:1]:
                    order = self._contract_matcher.execute_match(match, size)
                    if order and order.success:
                        kalshi_tid = f"news-kal-{int(time.time()*1000)}"
                        self._trade_count += 1
                        self._news_positions.append({
                            "entry_time": time.time(),
                            "venue": "kalshi",
                            "asset": match.ticker,
                            "side": match.side,
                            "size_usd": size,
                            "entry_price": order.filled_price,
                            "hold_until": time.time() + action.hold_minutes * 60,
                            "headline": news.headline[:80],
                            "trade_id": kalshi_tid,
                        })
                        shared_state.record_trade_opened(
                            trade_id=kalshi_tid, strategy="NEWS", side=match.side,
                            asset=match.ticker, venue="Kalshi",
                            entry_price=order.filled_price, size_usd=size,
                            confidence=action.confidence, reason=news.headline[:80],
                        )
                        shared_state.record_news(news.headline, news.source, news.priority, news.category)
                        logger.info(
                            "KALSHI TRADE: %s %s $%.2f (matched=%s, conf=%.2f)",
                            match.side, match.ticker, size, match.keywords_matched, action.confidence,
                        )
                        return True

            elif action.venue == "binance_futures":
                leveraged_size = size * action.leverage
                if action.side in ("LONG", "BUY"):
                    result = self._exchange.buy(action.asset, leveraged_size)
                else:
                    result = self._exchange.sell(action.asset, leveraged_size)
                if result.success:
                    fut_tid = f"news-fut-{int(time.time()*1000)}"
                    self._trade_count += 1
                    self._news_positions.append({
                        "entry_time": time.time(),
                        "venue": "binance_futures",
                        "asset": action.asset,
                        "side": action.side,
                        "size_usd": leveraged_size,
                        "margin": size,
                        "leverage": action.leverage,
                        "entry_price": result.filled_price,
                        "hold_until": time.time() + action.hold_minutes * 60,
                        "headline": news.headline[:80],
                        "trade_id": fut_tid,
                    })
                    shared_state.record_trade_opened(
                        trade_id=fut_tid, strategy="NEWS", side=action.side,
                        asset=action.asset, venue="Binance Futures",
                        entry_price=result.filled_price, size_usd=leveraged_size,
                        confidence=action.confidence, reason=f"{action.leverage}x leverage: {news.headline[:60]}",
                    )
                    shared_state.record_news(news.headline, news.source, news.priority, news.category)
                    logger.info(
                        "FUTURES TRADE: %s %s $%.2f (%dx leverage, margin=$%.2f, conf=%.2f)",
                        action.side, action.asset, leveraged_size, action.leverage,
                        size, action.confidence,
                    )
                    return True

        except Exception as exc:
            logger.error("Failed to execute %s on %s: %s", action.side, action.venue, exc)
        return False

    async def _news_exit_monitor(self) -> None:
        """Exit news-driven trades after hold period."""
        logger.info("News exit monitor started")
        while self._running:
            try:
                for pos in list(self._news_positions):
                    if time.time() >= pos["hold_until"]:
                        venue = pos["venue"]
                        size = pos.get("margin", pos["size_usd"])

                        if venue == "binance" or venue == "binance_futures":
                            if pos["side"] in ("BUY", "LONG"):
                                self._exchange.sell(pos["asset"], pos["size_usd"])
                            else:
                                self._exchange.buy(pos["asset"], pos["size_usd"])
                        elif venue == "alpaca":
                            self._stock_trader.close_position(pos["asset"])

                        # Simulate P&L in paper mode
                        pnl = size * random.uniform(-0.02, 0.04)
                        won = pnl > 0
                        if won:
                            self._win_count += 1
                        self._available_balance += size + pnl
                        self._portfolio_value += pnl
                        self._risk_manager.record_trade_result(pnl, source_wallet=venue)

                        # Record to shared state
                        news_trade_id = pos.get("trade_id", f"news-exit-{int(time.time()*1000)}")
                        shared_state.record_trade_closed(
                            trade_id=news_trade_id,
                            pnl=pnl,
                            reason=f"Hold expired: {pos.get('headline', '')[:40]}",
                        )
                        shared_state.update_portfolio(self._portfolio_value)

                        logger.info(
                            "NEWS EXIT: %s %s %s pnl=$%+.2f (held %dm, headline=%s)",
                            "WIN" if won else "LOSS", pos["venue"], pos["asset"],
                            pnl, (time.time() - pos["entry_time"]) / 60,
                            pos.get("headline", "")[:40],
                        )
                        self._news_positions.remove(pos)

                await asyncio.sleep(10)
            except Exception as exc:
                logger.error("News exit error: %s", exc, exc_info=True)
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


def _print_startup_status() -> None:
    """Show what's configured and what's missing."""
    print("=" * 60)
    print("  KALSHI MULTI-STRATEGY TRADING BOT v5.0")
    print("=" * 60)
    print(f"  Mode:      {'PAPER (simulated)' if config.PAPER_MODE else 'LIVE (real money)'}")
    print(f"  Balance:   ${config.PAPER_INITIAL_BALANCE_USDC:,.2f}")
    print(f"  Kalshi:    {'DEMO API' if config.KALSHI_USE_DEMO else 'PRODUCTION API'}")
    print()

    checks = [
        ("Kalshi API", bool(config.KALSHI_API_KEY_ID), "Contracts + arb trading"),
        ("Binance API", bool(config.BINANCE_API_KEY), "BTC/ETH spot trading"),
        ("Alpaca API", bool(config.ALPACA_API_KEY), "US stock trading"),
        ("Claude API", bool(config.ANTHROPIC_API_KEY), "AI sentiment analysis"),
        ("Telegram", bool(config.TELEGRAM_BOT_TOKEN), "Trade notifications"),
    ]

    print("  API Keys:")
    for name, configured, desc in checks:
        status = "READY" if configured else "NOT SET (paper mode)"
        icon = "+" if configured else "-"
        print(f"    [{icon}] {name:12s} {status:30s} {desc}")

    strategies = [
        "1. LATENCY ARB:   CEX price vs Kalshi crypto contracts",
        "2. TRUMP NEWS:    Truth Social → Claude → BTC + Kalshi",
        "3. BREAKING NEWS:  Reuters/AP/Fed → stocks + BTC + Kalshi",
        "4. KALSHI MATCH:   Any news → matching Kalshi contracts",
    ]
    print()
    print("  Strategies:")
    for s in strategies:
        print(f"    {s}")

    print()
    print("  Dashboard: Start with 'python dashboard.py' → http://localhost:5050")
    print("  Or use:    ./start.sh (starts both)")
    print("=" * 60)
    print()


if __name__ == "__main__":
    _print_startup_status()
    asyncio.run(main())
