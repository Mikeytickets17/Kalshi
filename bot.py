"""
Kalshi Longshot Bias Trading Bot — Main loop.

Orchestrates market scanning, signal evaluation, position management,
risk management, and notifications.
"""

import asyncio
import logging
import random
import signal
import sys
import time
from typing import Optional

import config
from kalshi import KalshiClient, OrderResult, Position, Side
from market_scanner import MarketOpportunity, MarketScanner
from notifier import TelegramNotifier
from position_sizer import PositionSizer
from risk_manager import RiskManager
from signal_evaluator import EvaluationResult, SignalEvaluator

# --- Logging Setup ---

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(config.LOG_FILE, mode="a"),
    ],
)
logger = logging.getLogger("bot")


class LongshotBiasBot:
    """Main bot that exploits longshot and favorite bias on Kalshi."""

    def __init__(self) -> None:
        self._paper_mode = config.PAPER_MODE
        self._running = False

        # Portfolio state
        initial_balance = config.PAPER_INITIAL_BALANCE_USDC if self._paper_mode else 0.0
        self._portfolio_value = initial_balance
        self._available_balance = initial_balance
        self._active_positions: dict[str, Position] = {}

        # Components
        self._client = KalshiClient()
        self._scanner = MarketScanner(self._client)
        self._evaluator = SignalEvaluator(self._client, self._active_positions)
        self._sizer = PositionSizer(self._portfolio_value)
        self._risk_manager = RiskManager(self._portfolio_value)
        self._notifier = TelegramNotifier()

        logger.info(
            "LongshotBiasBot initialized: paper_mode=%s demo=%s portfolio=$%.2f",
            self._paper_mode, config.KALSHI_USE_DEMO, self._portfolio_value,
        )

    async def run(self) -> None:
        """Main entry point — run the bot."""
        self._running = True
        logger.info("=" * 60)
        logger.info("Kalshi Longshot Bias Bot starting...")
        logger.info("Mode: %s", "PAPER" if self._paper_mode else "LIVE")
        logger.info("Environment: %s", "DEMO" if config.KALSHI_USE_DEMO else "PRODUCTION")
        logger.info("Portfolio: $%.2f", self._portfolio_value)
        logger.info("Scan interval: %ds", config.SCAN_INTERVAL_SECONDS)
        logger.info("Longshot max price: %.2f", config.LONGSHOT_MAX_PRICE)
        logger.info("Favorite min price: %.2f", config.FAVORITE_MIN_PRICE)
        logger.info("Signal threshold: %.2f", config.SIGNAL_THRESHOLD)
        logger.info("Max concurrent positions: %d", config.MAX_CONCURRENT_POSITIONS)
        logger.info("=" * 60)

        # Start concurrent tasks
        tasks = [
            asyncio.create_task(self._signal_processor(), name="signal_processor"),
            asyncio.create_task(self._exit_monitor(), name="exit_monitor"),
            asyncio.create_task(self._scanner.start(), name="market_scanner"),
        ]

        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for task in done:
                if task.exception():
                    logger.error("Task %s failed: %s", task.get_name(), task.exception())
        except asyncio.CancelledError:
            logger.info("Bot tasks cancelled")
        finally:
            await self.shutdown("Main loop ended")
            for task in tasks:
                if not task.done():
                    task.cancel()

    async def shutdown(self, reason: str = "Manual shutdown") -> None:
        """Graceful shutdown with cleanup."""
        if not self._running:
            return
        self._running = False
        logger.info("Shutting down: %s", reason)

        # Cancel all open orders
        self._client.cancel_all_orders()

        # Close all open positions and record PnL
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
                    "%s Closed %s: side=%s entry=%.4f current=%.4f pnl=$%.2f",
                    "[PAPER]" if self._paper_mode else "[LIVE]",
                    market_id, pos.side.value, pos.avg_price, pos.current_price, pnl,
                )
            self._active_positions.clear()

        # Send shutdown notification
        risk_summary = self._risk_manager.get_summary()
        self._notifier.notify_shutdown(reason, self._portfolio_value, risk_summary)

        # Cleanup
        await self._scanner.stop()
        self._client.close()
        self._notifier.close()

        logger.info("Shutdown complete. Final portfolio: $%.2f", self._portfolio_value)

    # --- Signal Processing ---

    async def _signal_processor(self) -> None:
        """Process market opportunities from the scanner."""
        logger.info("Signal processor started")
        while self._running:
            try:
                try:
                    opportunity = await asyncio.wait_for(
                        self._scanner.signal_queue.get(), timeout=5.0
                    )
                except asyncio.TimeoutError:
                    continue

                logger.info(
                    "Processing opportunity: %s %s @ %.4f (type=%s, edge=%.4f)",
                    opportunity.side, opportunity.ticker,
                    opportunity.current_price, opportunity.opportunity_type, opportunity.edge,
                )

                # Evaluate the opportunity
                evaluation = self._evaluator.evaluate(opportunity)

                if not evaluation.should_copy:
                    self._notifier.notify_signal_rejected(evaluation)
                    continue

                # Check risk limits
                can_trade, risk_reason = self._risk_manager.check_can_trade(
                    self._portfolio_value,
                    self._active_positions,
                    proposed_category=evaluation.market_info.category if evaluation.market_info else "",
                    source_wallet=opportunity.opportunity_type,
                )
                if not can_trade:
                    logger.warning("Risk check failed: %s", risk_reason)
                    self._notifier.notify_risk_alert(risk_reason)
                    continue

                # Compute position size
                self._sizer.portfolio_value = self._portfolio_value
                size_usdc = self._sizer.compute_size(evaluation)

                if size_usdc > self._available_balance:
                    logger.warning(
                        "Insufficient balance: need $%.2f, have $%.2f",
                        size_usdc, self._available_balance,
                    )
                    continue

                # Execute the trade
                await self._execute_trade(evaluation, size_usdc)

            except Exception as exc:
                logger.error("Signal processor error: %s", exc, exc_info=True)
                await asyncio.sleep(1)

    async def _execute_trade(
        self, evaluation: EvaluationResult, size_usdc: float
    ) -> None:
        """Execute a trade based on the evaluation."""
        opportunity = evaluation.signal
        ticker = opportunity.ticker

        # Place limit order — NEVER market orders
        result: OrderResult = self._client.place_order(
            ticker=ticker,
            side=evaluation.side,
            size_usdc=size_usdc,
            price=evaluation.target_price,
        )

        if not result.success:
            logger.error("Order failed: %s", result.error)
            return

        # Track the position
        position = Position(
            market_id=opportunity.market_id,
            condition_id=ticker,
            side=evaluation.side,
            size=result.filled_size or size_usdc,
            avg_price=result.filled_price or evaluation.target_price,
            current_price=result.filled_price or evaluation.target_price,
            source_wallet=opportunity.opportunity_type,
            category=opportunity.category,
        )
        self._active_positions[opportunity.market_id] = position
        self._available_balance -= size_usdc

        self._notifier.notify_trade_opened(
            evaluation, size_usdc, result.filled_price or evaluation.target_price
        )

        logger.info(
            "Position opened: %s %s $%.2f @ %.4f (type=%s, positions: %d/%d)",
            evaluation.side.value, ticker, size_usdc,
            result.filled_price or evaluation.target_price,
            opportunity.opportunity_type,
            len(self._active_positions), config.MAX_CONCURRENT_POSITIONS,
        )

    # --- Exit Monitoring ---

    async def _exit_monitor(self) -> None:
        """Monitor open positions for exit conditions."""
        logger.info("Exit monitor started")
        while self._running:
            try:
                for market_id, position in list(self._active_positions.items()):
                    should_exit, reason = await self._check_exit(position)
                    if should_exit:
                        await self._close_position(market_id, position, reason)

                await asyncio.sleep(config.EXIT_CHECK_INTERVAL_SECONDS)
            except Exception as exc:
                logger.error("Exit monitor error: %s", exc, exc_info=True)
                await asyncio.sleep(5)

    async def _check_exit(self, position: Position) -> tuple[bool, str]:
        """Check all exit conditions for a position."""
        # Update current price
        if self._paper_mode:
            drift = random.uniform(-0.01, 0.01)
            position.current_price = max(0.01, min(0.99, position.current_price + drift))
        else:
            price = self._client.get_price(position.condition_id)
            if price is not None:
                if position.side == Side.YES:
                    position.current_price = price
                else:
                    position.current_price = 1.0 - price

        # Update unrealized PnL
        if position.side == Side.YES:
            position.unrealized_pnl = (position.current_price - position.avg_price) * position.size
        else:
            position.unrealized_pnl = (position.avg_price - position.current_price) * position.size

        # Exit condition 1: Risk manager stop loss or emergency halt
        should_exit, reason = self._risk_manager.check_exit_conditions(
            position, self._portfolio_value
        )
        if should_exit:
            return True, reason

        # Exit condition 2: Time-based exit (near expiry + profitable)
        if self._client.is_connected and position.condition_id:
            market = self._client.get_market(position.condition_id)
            if market and market.end_date_ts > 0:
                time_remaining = market.end_date_ts - time.time()
                if time_remaining < config.EXIT_TIME_BUFFER_SECONDS and position.unrealized_pnl > 0:
                    return True, f"Time exit: {time_remaining:.0f}s left, locking ${position.unrealized_pnl:.2f} gain"

        return False, ""

    async def _close_position(
        self, market_id: str, position: Position, reason: str
    ) -> None:
        """Close an open position."""
        pnl = position.unrealized_pnl

        if not self._paper_mode:
            # Place closing order: sell what we hold
            close_side = Side.NO if position.side == Side.YES else Side.YES
            self._client.place_order(
                ticker=position.condition_id,
                side=close_side,
                size_usdc=position.size,
                price=position.current_price,
            )

        # Update state
        self._available_balance += position.size + pnl
        self._portfolio_value += pnl
        self._risk_manager.record_trade_result(pnl, source_wallet=position.source_wallet)
        self._sizer.portfolio_value = self._portfolio_value

        del self._active_positions[market_id]

        self._notifier.notify_trade_closed(position, pnl, reason)

        logger.info(
            "Position closed: %s pnl=$%.2f reason=%s (positions: %d/%d)",
            market_id, pnl, reason,
            len(self._active_positions), config.MAX_CONCURRENT_POSITIONS,
        )


async def main() -> None:
    """Entry point for the bot."""
    bot = LongshotBiasBot()

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
    print("Starting Kalshi Longshot Bias Trading Bot...")
    print(f"Mode: {'PAPER' if config.PAPER_MODE else 'LIVE'}")
    print(f"Environment: {'DEMO' if config.KALSHI_USE_DEMO else 'PRODUCTION'}")
    print(f"Paper balance: ${config.PAPER_INITIAL_BALANCE_USDC:,.2f}")
    asyncio.run(main())
