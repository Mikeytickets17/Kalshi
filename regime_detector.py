"""
Market Regime Detector.

Analyzes all recent headlines to determine the overall market direction.
The bot only trades in the direction of the regime — no more buying
and selling the same asset simultaneously.

BULLISH regime → only BUY/LONG signals accepted, SELL/SHORT rejected
BEARISH regime → only SELL/SHORT signals accepted, BUY/LONG rejected
NEUTRAL regime → both accepted but at half size

The regime updates every time a new headline is processed, using a
rolling window of the last 4 hours of signals.
"""

import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class RegimeState:
    direction: str  # "BULLISH", "BEARISH", "NEUTRAL"
    confidence: float  # 0-1
    bullish_count: int
    bearish_count: int
    last_updated: float
    reason: str


class RegimeDetector:
    """Tracks the macro market direction from headline flow."""

    BULLISH_KEYWORDS = [
        "ceasefire", "peace deal", "peace agreement", "peace talks",
        "rate cut", "cuts rate", "fed cut", "dovish",
        "rally", "surge", "soar", "jump", "record high",
        "oil plunge", "oil crash", "oil drop", "oil tumble",
        "deal signed", "trade deal", "agreement reached",
        "etf approved", "bitcoin reserve", "crypto executive order",
        "de-escalat", "troops withdraw", "suspend attack",
        "beat expectations", "strong earnings", "jobs beat",
        "inflation cool", "inflation ease", "inflation below",
        "pce below", "pce cool", "cpi below", "cpi cool",
        "Don't worry",
    ]

    BEARISH_KEYWORDS = [
        "tariff", "trade war", "sanctions imposed",
        "rate hike", "hawkish", "restrictive",
        "crash", "plunge", "selloff", "tumble", "tank",
        "invasion", "military strike", "troops deployed",
        "war ", "nuclear", "bomb", "attack",
        "escalat", "blockade", "strait closed",
        "miss expectations", "weak earnings", "jobs miss",
        "inflation hot", "inflation surge", "inflation above",
        "pce above", "pce hot", "cpi above", "cpi hot",
        "recession", "gdp negative", "shutdown",
        "impeach", "fire the fed",
    ]

    # Trump TACO pattern — extreme threats are actually BULLISH
    TACO_KEYWORDS = [
        "destroy", "obliterate", "annihilate",
        "civilization will die", "fire and fury",
        "like never before", "like the world has never seen",
        "total destruction", "wipe out", "devastat",
        "extremely hard", "pay a big price",
    ]

    def __init__(self, window_hours: float = 4.0) -> None:
        self._window = window_hours * 3600
        self._signals: list[dict] = []  # {time, direction, weight, headline}
        self._regime = RegimeState(
            direction="NEUTRAL", confidence=0.0,
            bullish_count=0, bearish_count=0,
            last_updated=time.time(), reason="No data yet",
        )

    @property
    def regime(self) -> RegimeState:
        return self._regime

    @property
    def direction(self) -> str:
        return self._regime.direction

    def process_headline(self, headline: str, source: str = "") -> None:
        """Process a headline and update the regime."""
        h = headline.lower()
        bull_hits = sum(1 for kw in self.BULLISH_KEYWORDS if kw in h)
        bear_hits = sum(1 for kw in self.BEARISH_KEYWORDS if kw in h)

        # TACO detection — extreme Trump threats are actually bullish
        taco_hits = sum(1 for kw in self.TACO_KEYWORDS if kw in h)
        if taco_hits > 0:
            bull_hits += taco_hits * 2  # double weight — TACO is strong signal
            logger.info("TACO DETECTED: '%s' — extreme threat = deal incoming = BULLISH", headline[:60])

        if bull_hits > bear_hits:
            direction = "BULLISH"
            weight = min(bull_hits - bear_hits, 3)
        elif bear_hits > bull_hits:
            direction = "BEARISH"
            weight = min(bear_hits - bull_hits, 3)
        else:
            return  # no signal, skip

        self._signals.append({
            "time": time.time(),
            "direction": direction,
            "weight": weight,
            "headline": headline[:80],
        })

        self._update_regime()

    def _update_regime(self) -> None:
        """Recalculate regime from recent signals."""
        now = time.time()
        cutoff = now - self._window

        # Remove old signals
        self._signals = [s for s in self._signals if s["time"] > cutoff]

        if not self._signals:
            self._regime = RegimeState(
                direction="NEUTRAL", confidence=0.0,
                bullish_count=0, bearish_count=0,
                last_updated=now, reason="No recent signals",
            )
            return

        bull_score = sum(s["weight"] for s in self._signals if s["direction"] == "BULLISH")
        bear_score = sum(s["weight"] for s in self._signals if s["direction"] == "BEARISH")
        total = bull_score + bear_score

        bull_count = sum(1 for s in self._signals if s["direction"] == "BULLISH")
        bear_count = sum(1 for s in self._signals if s["direction"] == "BEARISH")

        if total == 0:
            direction = "NEUTRAL"
            confidence = 0.0
            reason = "No weighted signals"
        elif bull_score > bear_score * 1.5:
            direction = "BULLISH"
            confidence = min(bull_score / total, 0.95)
            reason = f"{bull_count} bullish vs {bear_count} bearish signals (score: {bull_score:.0f} vs {bear_score:.0f})"
        elif bear_score > bull_score * 1.5:
            direction = "BEARISH"
            confidence = min(bear_score / total, 0.95)
            reason = f"{bear_count} bearish vs {bull_count} bullish signals (score: {bear_score:.0f} vs {bull_score:.0f})"
        else:
            direction = "NEUTRAL"
            confidence = 0.3
            reason = f"Mixed signals: {bull_count} bull vs {bear_count} bear"

        old_direction = self._regime.direction
        self._regime = RegimeState(
            direction=direction, confidence=confidence,
            bullish_count=bull_count, bearish_count=bear_count,
            last_updated=now, reason=reason,
        )

        if direction != old_direction:
            logger.info("REGIME CHANGE: %s → %s — %s", old_direction, direction, reason)

    def should_take_trade(self, trade_side: str) -> tuple[bool, str]:
        """Check if a trade direction is allowed in the current regime.

        Returns (allowed, reason).
        """
        side_upper = trade_side.upper()
        is_buy = side_upper in ("BUY", "LONG", "YES", "BUY_BOTH")
        is_sell = side_upper in ("SELL", "SHORT", "NO", "SELL_BOTH")

        if self._regime.direction == "BULLISH":
            if is_sell:
                return False, f"REGIME BLOCKED: {trade_side} rejected — market is BULLISH ({self._regime.reason})"
            return True, f"REGIME APPROVED: {trade_side} — aligned with BULLISH regime"

        elif self._regime.direction == "BEARISH":
            if is_buy:
                return False, f"REGIME BLOCKED: {trade_side} rejected — market is BEARISH ({self._regime.reason})"
            return True, f"REGIME APPROVED: {trade_side} — aligned with BEARISH regime"

        else:  # NEUTRAL
            return True, f"REGIME NEUTRAL: {trade_side} allowed (mixed signals)"

    def get_dashboard_data(self) -> dict:
        """Return data for the dashboard."""
        return {
            "direction": self._regime.direction,
            "confidence": round(self._regime.confidence, 2),
            "bullish_count": self._regime.bullish_count,
            "bearish_count": self._regime.bearish_count,
            "reason": self._regime.reason,
            "signal_count": len(self._signals),
        }
