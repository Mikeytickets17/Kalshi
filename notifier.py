"""
Notifier module.

Sends rich Telegram alerts for every trade event, Trump post detection,
news signal, risk warning, and daily summary. Supports message queuing
so burst events don't get rate-limited.
"""

import logging
import time
import threading
from collections import deque
from typing import Optional

import httpx

import config
from polymarket import Position
from signal_evaluator import EvaluationResult

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Sends notifications via Telegram Bot API with rate-limit protection."""

    def __init__(self) -> None:
        self._token = config.TELEGRAM_BOT_TOKEN
        self._chat_id = config.TELEGRAM_CHAT_ID
        self._enabled = bool(self._token and self._chat_id)
        self._http = httpx.Client(timeout=15.0)
        self._msg_queue: deque[str] = deque(maxlen=50)
        self._last_send: float = 0
        self._min_interval: float = 1.0  # Telegram rate limit: ~30 msg/sec

        if self._enabled:
            logger.info("Telegram notifier enabled (chat_id=%s)", self._chat_id)
        else:
            logger.info("Telegram notifier disabled (no token/chat_id configured)")

    def _send(self, text: str, parse_mode: str = "HTML") -> bool:
        """Send a message via Telegram with rate-limit protection."""
        if not self._enabled:
            logger.debug("[NOTIFIER] %s", text[:200])
            return False

        # Rate limit: wait if sending too fast
        elapsed = time.time() - self._last_send
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)

        try:
            resp = self._http.post(
                f"https://api.telegram.org/bot{self._token}/sendMessage",
                json={
                    "chat_id": self._chat_id,
                    "text": text,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": True,
                },
            )
            self._last_send = time.time()
            if resp.status_code == 429:
                retry_after = resp.json().get("parameters", {}).get("retry_after", 5)
                logger.warning("Telegram rate limited, retry in %ds", retry_after)
                time.sleep(retry_after)
                return self._send(text, parse_mode)
            resp.raise_for_status()
            return True
        except Exception as exc:
            logger.error("Telegram send failed: %s", exc)
            return False

    # ─── Trade Alerts ───

    def notify_trade_opened(
        self,
        evaluation: EvaluationResult,
        size_usdc: float,
        filled_price: float,
    ) -> None:
        """Notify when an ARB trade is opened."""
        signal = evaluation.signal
        market_name = evaluation.market_info.question if evaluation.market_info else signal.market_id
        mode = "[PAPER] " if config.PAPER_MODE else ""
        text = (
            f"<b>📈 {mode}ARB Trade Opened</b>\n\n"
            f"<b>Market:</b> {market_name}\n"
            f"<b>Side:</b> {evaluation.side.value}\n"
            f"<b>Size:</b> ${size_usdc:.2f}\n"
            f"<b>Price:</b> {filled_price:.4f}\n"
            f"<b>Edge:</b> {signal.edge*100:.1f}%\n"
            f"<b>Confidence:</b> {evaluation.confidence_score:.2f}\n"
            f"<b>Latency:</b> {signal.latency_ms:.0f}ms"
        )
        self._send(text)

    def notify_trade_closed(
        self,
        position: Position,
        pnl: float,
        reason: str,
    ) -> None:
        """Notify when an ARB position is closed."""
        emoji = "✅" if pnl >= 0 else "❌"
        mode = "[PAPER] " if config.PAPER_MODE else ""
        text = (
            f"<b>{emoji} {mode}ARB Trade Closed</b>\n\n"
            f"<b>Market:</b> {position.market_id}\n"
            f"<b>Side:</b> {position.side.value}\n"
            f"<b>Entry:</b> {position.avg_price:.4f}\n"
            f"<b>Exit:</b> {position.current_price:.4f}\n"
            f"<b>P&L:</b> <b>${pnl:+.2f}</b>\n"
            f"<b>Reason:</b> {reason}"
        )
        self._send(text)

    # ─── Trump Strategy Alerts ───

    def notify_trump_post_detected(
        self,
        post_text: str,
        source: str,
        sentiment: str,
        confidence: float,
        latency_ms: float = 0,
    ) -> None:
        """Alert when a new Trump post is detected."""
        emoji = "🟢" if sentiment.upper() == "BULLISH" else "🔴" if sentiment.upper() == "BEARISH" else "⚪"
        mode = "[PAPER] " if config.PAPER_MODE else ""
        text = (
            f"<b>🗣️ {mode}Trump Post Detected</b>\n\n"
            f"<i>\"{post_text[:200]}\"</i>\n\n"
            f"<b>Source:</b> {source}\n"
            f"<b>Sentiment:</b> {emoji} {sentiment}\n"
            f"<b>Confidence:</b> {confidence:.0%}\n"
            f"<b>Detection:</b> {latency_ms:.0f}ms"
        )
        self._send(text)

    def notify_trump_trade(
        self,
        side: str,
        asset: str,
        venue: str,
        size_usd: float,
        entry_price: float,
        confidence: float,
        post_text: str,
    ) -> None:
        """Alert when a Trump-driven trade is executed."""
        mode = "[PAPER] " if config.PAPER_MODE else ""
        text = (
            f"<b>⚡ {mode}TRUMP Trade</b>\n\n"
            f"<b>{side} {asset}</b> on {venue}\n"
            f"<b>Size:</b> ${size_usd:.2f}\n"
            f"<b>Entry:</b> {'$' if entry_price > 1 else ''}{entry_price:.4f}\n"
            f"<b>Confidence:</b> {confidence:.0%}\n"
            f"<b>Trigger:</b> <i>\"{post_text[:100]}\"</i>"
        )
        self._send(text)

    def notify_trump_exit(
        self,
        side: str,
        asset: str,
        pnl: float,
        entry_price: float,
        exit_price: float,
        hold_minutes: int,
    ) -> None:
        """Alert when a Trump trade exits."""
        emoji = "✅" if pnl >= 0 else "❌"
        mode = "[PAPER] " if config.PAPER_MODE else ""
        text = (
            f"<b>{emoji} {mode}TRUMP Trade Closed</b>\n\n"
            f"<b>{side} {asset}</b>\n"
            f"<b>Entry:</b> ${entry_price:,.2f}\n"
            f"<b>Exit:</b> ${exit_price:,.2f}\n"
            f"<b>P&L:</b> <b>${pnl:+.2f}</b>\n"
            f"<b>Held:</b> {hold_minutes}m"
        )
        self._send(text)

    # ─── News Strategy Alerts ───

    def notify_news_signal(
        self,
        headline: str,
        source: str,
        priority: str,
        category: str,
        actions_count: int,
    ) -> None:
        """Alert when a critical news headline triggers trades."""
        pri_emoji = "🚨" if priority == "critical" else "📰"
        mode = "[PAPER] " if config.PAPER_MODE else ""
        text = (
            f"<b>{pri_emoji} {mode}News Signal [{priority.upper()}]</b>\n\n"
            f"<b>\"{headline[:150]}\"</b>\n\n"
            f"<b>Source:</b> {source}\n"
            f"<b>Category:</b> {category}\n"
            f"<b>Trades:</b> {actions_count} actions triggered"
        )
        self._send(text)

    def notify_news_trade(
        self,
        side: str,
        asset: str,
        venue: str,
        size_usd: float,
        entry_price: float,
        confidence: float,
        headline: str,
    ) -> None:
        """Alert when a news-driven trade is executed."""
        mode = "[PAPER] " if config.PAPER_MODE else ""
        text = (
            f"<b>📰 {mode}NEWS Trade</b>\n\n"
            f"<b>{side} {asset}</b> on {venue}\n"
            f"<b>Size:</b> ${size_usd:.2f}\n"
            f"<b>Entry:</b> {'$' if entry_price > 1 else ''}{entry_price:.4f}\n"
            f"<b>Confidence:</b> {confidence:.0%}\n"
            f"<b>Headline:</b> <i>\"{headline[:120]}\"</i>"
        )
        self._send(text)

    # ─── Risk & System Alerts ───

    def notify_risk_alert(self, message: str) -> None:
        """Notify about a risk management event."""
        text = (
            f"<b>⚠️ Risk Alert</b>\n\n"
            f"{message}\n"
            f"{'[PAPER]' if config.PAPER_MODE else ''}"
        )
        self._send(text)

    def notify_signal_rejected(self, evaluation: EvaluationResult) -> None:
        """Log rejected signals (debug only, don't spam Telegram)."""
        signal = evaluation.signal
        logger.debug("Signal rejected: %s - %s", signal.market_id, evaluation.rejection_reason)

    def notify_daily_summary(
        self,
        portfolio_value: float,
        daily_pnl: float,
        open_positions: int,
        risk_summary: dict,
    ) -> None:
        """Send daily performance summary."""
        mode = "[PAPER] " if config.PAPER_MODE else ""
        emoji = "📈" if daily_pnl >= 0 else "📉"
        text = (
            f"<b>{emoji} {mode}Daily Summary</b>\n\n"
            f"<b>Portfolio:</b> ${portfolio_value:,.2f}\n"
            f"<b>Daily P&L:</b> ${daily_pnl:+,.2f}\n"
            f"<b>Open Positions:</b> {open_positions}\n"
            f"<b>Total Trades:</b> {risk_summary.get('total_trades', 0)}\n"
            f"<b>Win Rate:</b> {risk_summary.get('win_rate_pct', 0):.1f}%\n"
            f"<b>Consecutive Losses:</b> {risk_summary.get('consecutive_losses', 0)}\n"
            f"<b>Status:</b> {'HALTED ⛔' if risk_summary.get('halted') else 'Active ✅'}"
        )
        self._send(text)

    def notify_shutdown(self, reason: str, portfolio_value: float, risk_summary: dict) -> None:
        """Send shutdown notification with final summary."""
        mode = "[PAPER] " if config.PAPER_MODE else ""
        text = (
            f"<b>🛑 {mode}Bot Shutdown</b>\n\n"
            f"<b>Reason:</b> {reason}\n"
            f"<b>Final Portfolio:</b> ${portfolio_value:,.2f}\n"
            f"<b>Total Trades:</b> {risk_summary.get('total_trades', 0)}\n"
            f"<b>Win Rate:</b> {risk_summary.get('win_rate_pct', 0):.1f}%"
        )
        self._send(text)

    def notify_startup(self, portfolio_value: float, mode: str) -> None:
        """Send bot startup notification."""
        text = (
            f"<b>🚀 Bot Started</b>\n\n"
            f"<b>Mode:</b> {mode}\n"
            f"<b>Portfolio:</b> ${portfolio_value:,.2f}\n"
            f"<b>Strategies:</b> ARB + TRUMP + NEWS + KALSHI\n"
            f"<b>Dashboard:</b> http://localhost:5050"
        )
        self._send(text)

    def close(self) -> None:
        """Close the HTTP client."""
        self._http.close()
