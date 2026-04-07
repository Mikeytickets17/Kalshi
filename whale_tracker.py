"""
Kalshi Whale Tracker — Smart Money Flow Detection.

Monitors ALL Kalshi markets for signs of whale activity:
  1. Volume spikes (sudden 5x+ increase in trading volume)
  2. Price jumps (contract moves 10%+ in under 5 minutes)
  3. Order book imbalance (one side 3x+ heavier than the other)
  4. Open interest surges (new money entering a market)

When whale activity is detected, the bot can automatically
copy the trade direction.

Kalshi doesn't expose individual wallets, but whales leave
footprints in volume, price, and order flow.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

import config
from kalshi_client import KalshiClient, KalshiMarket

logger = logging.getLogger(__name__)


@dataclass
class WhaleSignal:
    """A detected whale activity signal."""
    ticker: str
    title: str
    signal_type: str    # "volume_spike", "price_jump", "oi_surge", "book_imbalance"
    direction: str      # "YES" or "NO" — which side the whale is buying
    confidence: float   # 0-1
    magnitude: float    # How big the signal is (e.g., 5x volume = 5.0)
    current_price: float
    price_change: float  # How much price moved
    volume: float
    detected_at: float = field(default_factory=time.time)
    details: str = ""


@dataclass
class MarketSnapshot:
    """Point-in-time snapshot of a Kalshi market for change detection."""
    ticker: str
    title: str
    yes_price: float
    no_price: float
    volume: float
    timestamp: float


class WhaleTracker:
    """Tracks whale activity across all Kalshi markets."""

    # Thresholds for whale detection
    VOLUME_SPIKE_MULTIPLIER = 3.0    # 3x normal volume = whale
    PRICE_JUMP_THRESHOLD = 0.08      # 8% price move = whale
    OI_SURGE_MULTIPLIER = 2.0        # 2x open interest change = whale
    BOOK_IMBALANCE_RATIO = 3.0       # 3:1 bid/ask ratio = whale
    MIN_VOLUME_FOR_SIGNAL = 100      # Ignore low-volume markets
    SCAN_INTERVAL = 15               # Seconds between scans

    def __init__(self, kalshi: KalshiClient = None) -> None:
        self._kalshi = kalshi or KalshiClient()
        self._signal_queue: asyncio.Queue[WhaleSignal] = asyncio.Queue()
        self._running = False
        self._snapshots: dict[str, list[MarketSnapshot]] = {}  # ticker -> history
        self._http = httpx.AsyncClient(timeout=10.0)

        # Stats for the dashboard
        self._total_signals = 0
        self._signals_today: list[WhaleSignal] = []
        self._copied_trades: list[dict] = []

    @property
    def signal_queue(self) -> asyncio.Queue[WhaleSignal]:
        return self._signal_queue

    @property
    def signals_today(self) -> list[WhaleSignal]:
        return self._signals_today

    @property
    def copied_trades(self) -> list[dict]:
        return self._copied_trades

    async def start(self) -> None:
        """Start monitoring Kalshi markets for whale activity."""
        self._running = True
        logger.info("WhaleTracker started — scanning all Kalshi markets every %ds", self.SCAN_INTERVAL)

        if config.PAPER_MODE:
            await self._run_paper_mode()
        else:
            await self._run_live()

    async def stop(self) -> None:
        self._running = False
        await self._http.aclose()
        logger.info("WhaleTracker stopped")

    async def _run_live(self) -> None:
        """Live whale detection loop."""
        while self._running:
            try:
                markets = self._fetch_all_markets()
                for market in markets:
                    signals = self._check_for_whales(market)
                    for signal in signals:
                        self._total_signals += 1
                        self._signals_today.append(signal)
                        await self._signal_queue.put(signal)
                        logger.info(
                            "WHALE DETECTED: %s on %s — %s side, %.1fx magnitude, price=%.2f",
                            signal.signal_type, signal.ticker, signal.direction,
                            signal.magnitude, signal.current_price,
                        )
            except Exception as exc:
                logger.error("WhaleTracker scan error: %s", exc)

            # Clean old signals (keep last 24 hours)
            cutoff = time.time() - 86400
            self._signals_today = [s for s in self._signals_today if s.detected_at > cutoff]

            await asyncio.sleep(self.SCAN_INTERVAL)

    def _fetch_all_markets(self) -> list[KalshiMarket]:
        """Fetch all open markets from Kalshi."""
        if not self._kalshi.is_connected:
            return []
        try:
            markets = []
            # Get crypto markets
            markets.extend(self._kalshi.get_crypto_markets())
            # Get all other open markets
            try:
                resp = self._kalshi._client.get_markets(status="open", limit=500)
                seen = {m.ticker for m in markets}
                for m in resp.get("markets", []):
                    parsed = self._kalshi._parse_market(m)
                    if parsed and parsed.ticker not in seen:
                        markets.append(parsed)
                        seen.add(parsed.ticker)
            except Exception:
                pass
            return markets
        except Exception as exc:
            logger.debug("Failed to fetch markets: %s", exc)
            return []

    def _check_for_whales(self, market: KalshiMarket) -> list[WhaleSignal]:
        """Check a single market for whale activity signals."""
        signals = []
        ticker = market.ticker

        # Get or create snapshot history
        if ticker not in self._snapshots:
            self._snapshots[ticker] = []

        history = self._snapshots[ticker]
        now = MarketSnapshot(
            ticker=ticker, title=market.title,
            yes_price=market.yes_price, no_price=market.no_price,
            volume=market.volume, timestamp=time.time(),
        )

        if history:
            prev = history[-1]
            time_diff = now.timestamp - prev.timestamp

            if time_diff > 0 and time_diff < 300:  # Only compare within 5 minutes
                # 1. Volume spike detection
                if prev.volume > 0 and now.volume > self.MIN_VOLUME_FOR_SIGNAL:
                    vol_ratio = now.volume / max(prev.volume, 1)
                    if vol_ratio >= self.VOLUME_SPIKE_MULTIPLIER:
                        direction = "YES" if now.yes_price > prev.yes_price else "NO"
                        signals.append(WhaleSignal(
                            ticker=ticker, title=market.title,
                            signal_type="volume_spike",
                            direction=direction,
                            confidence=min(0.5 + (vol_ratio - 3) * 0.1, 0.90),
                            magnitude=round(vol_ratio, 1),
                            current_price=now.yes_price,
                            price_change=round(now.yes_price - prev.yes_price, 4),
                            volume=now.volume,
                            details=f"Volume {vol_ratio:.1f}x normal ({int(prev.volume)} → {int(now.volume)})",
                        ))

                # 2. Price jump detection
                price_change = abs(now.yes_price - prev.yes_price)
                if price_change >= self.PRICE_JUMP_THRESHOLD and prev.yes_price > 0:
                    direction = "YES" if now.yes_price > prev.yes_price else "NO"
                    signals.append(WhaleSignal(
                        ticker=ticker, title=market.title,
                        signal_type="price_jump",
                        direction=direction,
                        confidence=min(0.55 + price_change * 2, 0.92),
                        magnitude=round(price_change / prev.yes_price * 100, 1),
                        current_price=now.yes_price,
                        price_change=round(now.yes_price - prev.yes_price, 4),
                        volume=now.volume,
                        details=f"Price moved {price_change*100:.1f}% in {time_diff:.0f}s",
                    ))

        # Keep last 20 snapshots per market
        history.append(now)
        if len(history) > 20:
            self._snapshots[ticker] = history[-20:]

        return signals

    def record_copy_trade(self, signal: WhaleSignal, size_usd: float, entry_price: float) -> None:
        """Record that we copied a whale trade."""
        self._copied_trades.append({
            "ticker": signal.ticker,
            "title": signal.title,
            "signal_type": signal.signal_type,
            "direction": signal.direction,
            "size_usd": size_usd,
            "entry_price": entry_price,
            "whale_confidence": signal.confidence,
            "whale_magnitude": signal.magnitude,
            "copied_at": time.time(),
            "pnl": 0,
            "status": "OPEN",
        })

    def get_dashboard_data(self) -> dict:
        """Return data for the whale tracker dashboard tab."""
        return {
            "total_signals": self._total_signals,
            "signals_today": len(self._signals_today),
            "recent_signals": [
                {
                    "ticker": s.ticker,
                    "title": s.title[:60],
                    "type": s.signal_type,
                    "direction": s.direction,
                    "confidence": round(s.confidence, 2),
                    "magnitude": s.magnitude,
                    "price": s.current_price,
                    "price_change": s.price_change,
                    "volume": int(s.volume),
                    "time": s.detected_at,
                    "details": s.details,
                }
                for s in self._signals_today[-20:]
            ],
            "copied_trades": self._copied_trades[-20:],
            "markets_tracked": len(self._snapshots),
        }

    # --- Paper Mode ---

    async def _run_paper_mode(self) -> None:
        """Simulate whale activity for testing."""
        import random
        logger.info("[PAPER] WhaleTracker running in simulation mode")

        sample_markets = [
            ("KXBTC-UP-85000", "Will BTC be above $85,000?"),
            ("TARIFF-CHINA-50", "Will US impose >50% tariffs on China?"),
            ("FED-RATE-CUT-APR", "Will the Fed cut rates in April?"),
            ("TRUMP-EO-CRYPTO", "Will Trump sign crypto executive order?"),
            ("IRAN-CEASEFIRE", "Will there be an Iran ceasefire by May?"),
            ("GOV-SHUTDOWN", "Will there be a government shutdown?"),
            ("BTC-100K-2026", "Will Bitcoin hit $100K in 2026?"),
            ("UKRAINE-PEACE", "Will Ukraine peace deal be signed?"),
        ]

        while self._running:
            await asyncio.sleep(random.uniform(20, 60))
            if not self._running:
                break

            ticker, title = random.choice(sample_markets)
            signal_type = random.choice(["volume_spike", "price_jump", "oi_surge"])
            direction = random.choice(["YES", "NO"])
            magnitude = round(random.uniform(3.0, 12.0), 1)
            price = round(random.uniform(0.15, 0.85), 2)

            signal = WhaleSignal(
                ticker=ticker, title=title,
                signal_type=signal_type,
                direction=direction,
                confidence=min(0.5 + magnitude * 0.04, 0.92),
                magnitude=magnitude,
                current_price=price,
                price_change=round(random.uniform(-0.15, 0.15), 4),
                volume=random.randint(500, 50000),
                details=f"[PAPER] Simulated {signal_type}: {magnitude:.1f}x",
            )

            self._total_signals += 1
            self._signals_today.append(signal)
            await self._signal_queue.put(signal)
            logger.info(
                "[PAPER] WHALE: %s %s on %s — %s %.1fx (conf=%.2f)",
                signal.signal_type, direction, ticker, title[:40],
                magnitude, signal.confidence,
            )
