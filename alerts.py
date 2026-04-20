"""
Apprise-powered multi-channel alerts.

Sends trade alerts to ANY platform: Telegram, Discord, Slack, Email,
SMS, Pushover, ntfy, and 80+ more services.

Configure via APPRISE_URLS in .env — one URL per service:
  APPRISE_URLS=tgram://bottoken/ChatID,discord://webhook_id/webhook_token

Docs: https://github.com/caronc/apprise/wiki

pip install apprise
"""

import asyncio
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Try to import apprise
try:
    import apprise
    APPRISE_AVAILABLE = True
except ImportError:
    APPRISE_AVAILABLE = False
    logger.info("Apprise not installed — run: pip install apprise")


class AlertManager:
    """Multi-channel alert system powered by Apprise."""

    def __init__(self) -> None:
        self._enabled = False
        self._ap: Optional[object] = None
        self._alert_count = 0
        self._last_alert_time: float = 0
        self._min_interval = 10  # Minimum seconds between alerts (anti-spam)

        urls = os.getenv("APPRISE_URLS", "")
        if not urls or not APPRISE_AVAILABLE:
            if not APPRISE_AVAILABLE:
                logger.info("Alerts disabled — pip install apprise to enable")
            else:
                logger.info("Alerts disabled — set APPRISE_URLS in .env")
            return

        self._ap = apprise.Apprise()
        for url in urls.split(","):
            url = url.strip()
            if url:
                self._ap.add(url)

        self._enabled = True
        logger.info("Alerts enabled — %d notification channels configured", len(self._ap))

    def alert_trade_opened(
        self, strategy: str, side: str, asset: str, venue: str,
        size: float, entry_price: float, confidence: float, reason: str,
    ) -> None:
        """Alert when a trade is opened."""
        title = f"TRADE OPENED: {side} {asset}"
        body = (
            f"Strategy: {strategy}\n"
            f"Side: {side} | Asset: {asset} | Venue: {venue}\n"
            f"Size: ${size:.2f} | Entry: {entry_price}\n"
            f"Confidence: {confidence:.0%}\n"
            f"Reason: {reason}"
        )
        self._send(title, body)

    def alert_trade_closed(
        self, strategy: str, asset: str, pnl: float, result: str,
    ) -> None:
        """Alert when a trade is closed."""
        emoji = "WIN" if pnl > 0 else "LOSS"
        title = f"{emoji}: {asset} ${pnl:+.2f}"
        body = (
            f"Strategy: {strategy}\n"
            f"P&L: ${pnl:+.2f} | Result: {result}"
        )
        self._send(title, body)

    def alert_spread_detected(
        self, asset: str, spread: float, direction: str,
        spot: float, strike: float, question: str,
    ) -> None:
        """Alert when a Polymarket-Coinbase spread is detected."""
        title = f"SPREAD: {direction} {asset} {spread:.1%}"
        body = (
            f"Spot: ${spot:,.0f} | Strike: ${strike:,.0f}\n"
            f"Direction: {direction} | Spread: {spread:.1%}\n"
            f"Contract: {question[:60]}"
        )
        self._send(title, body)

    def alert_risk_event(self, event: str, details: str) -> None:
        """Alert on risk management events (halt, drawdown, etc.)."""
        title = f"RISK ALERT: {event}"
        self._send(title, details, notify_type="failure")

    def alert_bot_status(self, status: str, portfolio: float) -> None:
        """Alert on bot start/stop."""
        title = f"BOT {status}"
        body = f"Portfolio: ${portfolio:,.2f}"
        self._send(title, body)

    def _send(self, title: str, body: str, notify_type: str = "info") -> None:
        """Send alert to all configured channels."""
        if not self._enabled or not self._ap:
            return

        # Anti-spam: minimum interval between alerts
        now = time.time()
        if now - self._last_alert_time < self._min_interval:
            return

        try:
            nt = getattr(apprise.NotifyType, notify_type.upper(), apprise.NotifyType.INFO)
            self._ap.notify(title=title, body=body, notify_type=nt)
            self._alert_count += 1
            self._last_alert_time = now
        except Exception as exc:
            logger.debug("Alert send error: %s", exc)
