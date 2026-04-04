"""
Notifier module.

Sends alerts via Telegram for trade events, risk warnings, and daily summaries.
"""

import logging
from typing import Optional

import httpx

import config
from polymarket import Position
from signal_evaluator import EvaluationResult

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Sends notifications via Telegram Bot API."""

    def __init__(self) -> None:
        self._token = config.TELEGRAM_BOT_TOKEN
        self._chat_id = config.TELEGRAM_CHAT_ID
        self._enabled = bool(self._token and self._chat_id)
        self._http = httpx.Client(timeout=15.0)

        if self._enabled:
            logger.info("Telegram notifier enabled (chat_id=%s)", self._chat_id)
        else:
            logger.info("Telegram notifier disabled (no token/chat_id configured)")

    def _send(self, text: str, parse_mode: str = "HTML") -> bool:
        """Send a message via Telegram."""
        if not self._enabled:
            logger.debug("[NOTIFIER] %s", text)
            return False

        try:
            resp = self._http.post(
                f"https://api.telegram.org/bot{self._token}/sendMessage",
                json={
                    "chat_id": self._chat_id,
                    "text": text,
                    "parse_mode": parse_mode,
                },
            )
            resp.raise_for_status()
            return True
        except Exception as exc:
            logger.error("Telegram send failed: %s", exc)
            return False

    def notify_trade_opened(
        self,
        evaluation: EvaluationResult,
        size_usdc: float,
        filled_price: float,
    ) -> None:
        """Notify when a new copied trade is opened."""
        signal = evaluation.signal
        market_name = evaluation.market_info.question if evaluation.market_info else signal.market_id
        text = (
            f"<b>📈 Trade Opened</b>\n"
            f"Market: {market_name}\n"
            f"Side: {evaluation.side.value}\n"
            f"Size: ${size_usdc:.2f}\n"
            f"Price: {filled_price:.4f}\n"
            f"Confidence: {evaluation.confidence_score:.3f}\n"
            f"Source: {signal.wallet_alias} (WR: {signal.wallet_win_rate:.0%})\n"
            f"{'[PAPER]' if config.PAPER_MODE else ''}"
        )
        self._send(text)
        logger.info("Notified: trade opened %s %s $%.2f", evaluation.side.value, signal.market_id, size_usdc)

    def notify_trade_closed(
        self,
        position: Position,
        pnl: float,
        reason: str,
    ) -> None:
        """Notify when a position is closed."""
        emoji = "✅" if pnl >= 0 else "❌"
        text = (
            f"<b>{emoji} Trade Closed</b>\n"
            f"Market: {position.market_id}\n"
            f"Side: {position.side.value}\n"
            f"Entry: {position.avg_price:.4f}\n"
            f"Exit: {position.current_price:.4f}\n"
            f"PnL: ${pnl:+.2f}\n"
            f"Reason: {reason}\n"
            f"{'[PAPER]' if config.PAPER_MODE else ''}"
        )
        self._send(text)
        logger.info("Notified: trade closed %s pnl=$%.2f reason=%s", position.market_id, pnl, reason)

    def notify_risk_alert(self, message: str) -> None:
        """Notify about a risk management event."""
        text = f"<b>⚠️ Risk Alert</b>\n{message}\n{'[PAPER]' if config.PAPER_MODE else ''}"
        self._send(text)
        logger.warning("Risk alert: %s", message)

    def notify_signal_rejected(self, evaluation: EvaluationResult) -> None:
        """Notify when a signal is rejected (optional, for debugging)."""
        signal = evaluation.signal
        text = (
            f"<b>⏭️ Signal Skipped</b>\n"
            f"Market: {signal.market_id}\n"
            f"Wallet: {signal.wallet_alias}\n"
            f"Reason: {evaluation.rejection_reason}\n"
        )
        logger.debug("Signal rejected: %s - %s", signal.market_id, evaluation.rejection_reason)

    def notify_daily_summary(
        self,
        portfolio_value: float,
        daily_pnl: float,
        open_positions: int,
        risk_summary: dict,
    ) -> None:
        """Send daily performance summary."""
        text = (
            f"<b>📊 Daily Summary</b>\n"
            f"Portfolio: ${portfolio_value:,.2f}\n"
            f"Daily PnL: ${daily_pnl:+,.2f}\n"
            f"Open Positions: {open_positions}\n"
            f"Total Trades: {risk_summary.get('total_trades', 0)}\n"
            f"Win Rate: {risk_summary.get('win_rate_pct', 0):.1f}%\n"
            f"Consecutive Losses: {risk_summary.get('consecutive_losses', 0)}\n"
            f"Status: {'HALTED ⛔' if risk_summary.get('halted') else 'Active ✅'}\n"
            f"{'[PAPER]' if config.PAPER_MODE else ''}"
        )
        self._send(text)
        logger.info("Daily summary sent: portfolio=$%.2f pnl=$%.2f", portfolio_value, daily_pnl)

    def notify_shutdown(self, reason: str, portfolio_value: float, risk_summary: dict) -> None:
        """Send shutdown notification with final summary."""
        text = (
            f"<b>🛑 Bot Shutdown</b>\n"
            f"Reason: {reason}\n"
            f"Final Portfolio: ${portfolio_value:,.2f}\n"
            f"Total Trades: {risk_summary.get('total_trades', 0)}\n"
            f"Win Rate: {risk_summary.get('win_rate_pct', 0):.1f}%\n"
            f"{'[PAPER]' if config.PAPER_MODE else ''}"
        )
        self._send(text)
        logger.info("Shutdown notification sent")

    def close(self) -> None:
        """Close the HTTP client."""
        self._http.close()
