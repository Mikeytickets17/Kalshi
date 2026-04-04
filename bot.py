"""
Polymarket Copy-Trading Bot — Main loop.

Orchestrates wallet tracking, signal evaluation, position management,
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
from notifier import TelegramNotifier
from polymarket import OrderResult, PolymarketClient, Position, Side
from position_sizer import PositionSizer
from risk_manager import RiskManager
from signal_evaluator import EvaluationResult, SignalEvaluator
from wallet_ranker import WalletRanker
from wallet_tracker import TradeSignal, WalletTracker

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


class CopyTradingBot:
    """Main copy-trading bot that mirrors positions from high-performing wallets."""

    def __init__(self) -> None:
        self._paper_mode = config.PAPER_MODE
        self._running = False

        # Portfolio state
        initial_balance = config.PAPER_INITIAL_BALANCE_USDC if self._paper_mode else 0.0
        self._portfolio_value = initial_balance
        self._available_balance = initial_balance
        self._active_positions: dict[str, Position] = {}

        # Components
        self._client = PolymarketClient()
        self._tracker = WalletTracker()
        self._evaluator = SignalEvaluator(self._client, self._active_positions)
        self._sizer = PositionSizer(self._portfolio_value)
        self._risk_manager = RiskManager(self._portfolio_value)
        self._ranker = WalletRanker()
        self._notifier = TelegramNotifier()

        logger.info(
            "CopyTradingBot initialized: paper_mode=%s portfolio=$%.2f",
            self._paper_mode, self._portfolio_value,
        )

    async def run(self) -> None:
        """Main entry point — run the bot."""
        self._running = True
        logger.info("=" * 60)
        logger.info("Polymarket Copy-Trading Bot starting...")
        logger.info("Mode: %s", "PAPER" if self._paper_mode else "LIVE")
        logger.info("Portfolio: $%.2f", self._portfolio_value)
        logger.info("Watched wallets: %d", len(self._tracker.get_active_addresses()))
        logger.info("Copy threshold: %.2f", config.COPY_THRESHOLD)
        logger.info("Max concurrent positions: %d", config.MAX_CONCURRENT_POSITIONS)
        logger.info("=" * 60)

        # Run initial wallet ranking if needed
        if self._ranker.should_run():
            try:
                self._ranker.run()
                self._tracker.load_watchlist()
            except Exception as exc:
                logger.warning("Initial wallet ranking failed: %s", exc)

        # Start concurrent tasks
        tasks = [
            asyncio.create_task(self._signal_processor(), name="signal_processor"),
            asyncio.create_task(self._exit_monitor(), name="exit_monitor"),
            asyncio.create_task(self._wallet_ranker_loop(), name="wallet_ranker"),
            asyncio.create_task(self._tracker.start(), name="wallet_tracker"),
        ]

        try:
            # Wait for any task to complete (which means an error or shutdown)
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

        # Close all positions in paper mode (log only)
        if self._paper_mode and self._active_positions:
            logger.info("Closing %d paper positions...", len(self._active_positions))
            for market_id, pos in list(self._active_positions.items()):
                pnl = (pos.current_price - pos.avg_price) * pos.size
                logger.info(
                    "[PAPER] Closed %s: entry=%.4f current=%.4f pnl=$%.2f",
                    market_id, pos.avg_price, pos.current_price, pnl,
                )

        # Send shutdown notification
        risk_summary = self._risk_manager.get_summary()
        self._notifier.notify_shutdown(reason, self._portfolio_value, risk_summary)

        # Cleanup
        await self._tracker.stop()
        self._client.close()
        self._ranker.close()
        self._notifier.close()

        logger.info("Shutdown complete. Final portfolio: $%.2f", self._portfolio_value)

    # --- Signal Processing ---

    async def _signal_processor(self) -> None:
        """Process trade signals from the wallet tracker."""
        logger.info("Signal processor started")
        while self._running:
            try:
                # Wait for a signal with timeout so we can check _running
                try:
                    signal = await asyncio.wait_for(
                        self._tracker.signal_queue.get(), timeout=5.0
                    )
                except asyncio.TimeoutError:
                    continue

                logger.info(
                    "Processing signal: wallet=%s market=%s side=%s size=$%.2f",
                    signal.wallet_alias, signal.market_id, signal.side, signal.size_usdc,
                )

                # Evaluate the signal
                evaluation = self._evaluator.evaluate(signal)

                if not evaluation.should_copy:
                    self._notifier.notify_signal_rejected(evaluation)
                    continue

                # Check risk limits
                can_trade, risk_reason = self._risk_manager.check_can_trade(
                    self._portfolio_value,
                    self._active_positions,
                    proposed_category=evaluation.market_info.category if evaluation.market_info else "",
                    source_wallet=signal.wallet_address,
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
                await self._execute_copy_trade(evaluation, size_usdc)

            except Exception as exc:
                logger.error("Signal processor error: %s", exc, exc_info=True)
                await asyncio.sleep(1)

    async def _execute_copy_trade(
        self, evaluation: EvaluationResult, size_usdc: float
    ) -> None:
        """Execute a copy trade based on the evaluation."""
        signal = evaluation.signal
        market_info = evaluation.market_info

        # Use condition_id as token_id for the order
        token_id = signal.condition_id or signal.market_id

        result: OrderResult = self._client.place_order(
            token_id=token_id,
            side=evaluation.side,
            size_usdc=size_usdc,
            price=evaluation.target_price,
        )

        if not result.success:
            logger.error("Order failed: %s", result.error)
            return

        # Track the position
        position = Position(
            market_id=signal.market_id,
            condition_id=signal.condition_id,
            side=evaluation.side,
            size=result.filled_size or size_usdc,
            avg_price=result.filled_price or evaluation.target_price,
            current_price=result.filled_price or evaluation.target_price,
            source_wallet=signal.wallet_address,
            category=market_info.category if market_info else "unknown",
        )
        self._active_positions[signal.market_id] = position
        self._available_balance -= size_usdc

        self._notifier.notify_trade_opened(
            evaluation, size_usdc, result.filled_price or evaluation.target_price
        )

        logger.info(
            "Position opened: market=%s side=%s size=$%.2f price=%.4f (positions: %d/%d)",
            signal.market_id, evaluation.side.value, size_usdc,
            result.filled_price or evaluation.target_price,
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
            # Simulate price movement in paper mode
            drift = random.uniform(-0.02, 0.02)
            position.current_price = max(0.01, min(0.99, position.current_price + drift))
        else:
            price = self._client.get_price(position.condition_id)
            if price is not None:
                position.current_price = price

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
        # In paper mode, skip time-based exits since we don't have real end dates

        # Exit condition 3: Wallet closed their position
        # (would require tracking the source wallet's position — simplified here)

        return False, ""

    async def _close_position(
        self, market_id: str, position: Position, reason: str
    ) -> None:
        """Close an open position."""
        pnl = position.unrealized_pnl

        if not self._paper_mode:
            # Place closing order
            close_side = Side.NO if position.side == Side.YES else Side.YES
            self._client.place_order(
                token_id=position.condition_id,
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
            "Position closed: market=%s pnl=$%.2f reason=%s (positions: %d/%d)",
            market_id, pnl, reason,
            len(self._active_positions), config.MAX_CONCURRENT_POSITIONS,
        )

    # --- Wallet Ranker Loop ---

    async def _wallet_ranker_loop(self) -> None:
        """Periodically re-rank wallets."""
        logger.info("Wallet ranker loop started (interval=%dh)", config.WALLET_REFRESH_INTERVAL_HOURS)
        while self._running:
            await asyncio.sleep(60)  # Check every minute
            if self._ranker.should_run():
                try:
                    logger.info("Running scheduled wallet ranking...")
                    self._ranker.run()
                    self._tracker.load_watchlist()
                except Exception as exc:
                    logger.error("Wallet ranking failed: %s", exc, exc_info=True)


async def main() -> None:
    """Entry point for the bot."""
    bot = CopyTradingBot()

    # Handle graceful shutdown via signals
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("Received shutdown signal")
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    # Run bot with shutdown monitoring
    bot_task = asyncio.create_task(bot.run())

    # Wait for either the bot to finish or a shutdown signal
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
    print("Starting Polymarket Copy-Trading Bot...")
    print(f"Mode: {'PAPER' if config.PAPER_MODE else 'LIVE'}")
    print(f"Paper balance: ${config.PAPER_INITIAL_BALANCE_USDC:,.2f}")
    asyncio.run(main())
