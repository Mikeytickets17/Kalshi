"""
Market scanner — 2 strategies only, quality over quantity.

Only the two strategies that backtest profitably:

1. LONGSHOT FADE: YES under 12c in ANY category → buy NO
   - Only when edge > 3c (survives the ~1.5c fee)
   - Cheaper longshots have bigger bias, so tighter ceiling

2. FAVORITE LEAN: YES above 75c in ANY category → buy YES
   - Only when edge > 2c
   - Higher floor means stronger favorites with more reliable bias

Killed strategies (backtest losers):
  - closing_drift: 49.6% WR, worse than coin flip
  - multi_arb: losses 4x wins despite 71% WR
  - stale_midrange: edge is indistinguishable from noise
"""

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Optional

import config
from kalshi import KalshiClient, MarketInfo

logger = logging.getLogger(__name__)


@dataclass
class MarketOpportunity:
    """A market opportunity detected by the scanner."""
    market_id: str
    ticker: str
    title: str
    category: str
    side: str
    current_price: float
    estimated_true_prob: float
    edge: float
    volume: float
    close_time_ts: int
    opportunity_type: str
    timestamp: float = field(default_factory=time.time)

    # Compatibility fields for position_sizer.py and notifier.py
    wallet_weight: float = 1.0
    wallet_alias: str = ""
    wallet_win_rate: float = 0.0
    wallet_portfolio_pct: float = 0.0


class MarketScanner:
    """Scans Kalshi for high-edge longshot and favorite opportunities."""

    def __init__(self, client: KalshiClient) -> None:
        self._client = client
        self._signal_queue: asyncio.Queue[MarketOpportunity] = asyncio.Queue()
        self._running: bool = False
        self._seen_markets: set[str] = set()

    @property
    def signal_queue(self) -> asyncio.Queue[MarketOpportunity]:
        return self._signal_queue

    async def start(self) -> None:
        self._running = True
        logger.info(
            "MarketScanner starting (scan=%ds, longshot<%.0fc min_edge>%.0fc, "
            "favorite>%.0fc min_edge>%.0fc, min_vol=$%d)",
            config.SCAN_INTERVAL_SECONDS,
            config.LONGSHOT_MAX_PRICE * 100, config.LONGSHOT_MIN_EDGE * 100,
            config.FAVORITE_MIN_PRICE * 100, config.FAVORITE_MIN_EDGE * 100,
            config.MIN_MARKET_VOLUME,
        )

        if config.PAPER_MODE and not self._client.is_connected:
            await self._run_paper_mode()
        else:
            await self._run_live_scan()

    async def stop(self) -> None:
        self._running = False
        logger.info("MarketScanner stopped")

    # --- Live Scanning ---

    async def _run_live_scan(self) -> None:
        while self._running:
            try:
                all_markets = await self._fetch_all_markets()
                opps_found = 0

                for market in all_markets:
                    opp = self._evaluate_market(market)
                    if opp and opp.market_id not in self._seen_markets:
                        self._seen_markets.add(opp.market_id)
                        await self._signal_queue.put(opp)
                        opps_found += 1
                        logger.info(
                            "Opportunity: %s %s @ %.2f edge=%.3f vol=$%.0f type=%s",
                            opp.side, opp.ticker, opp.current_price,
                            opp.edge, opp.volume, opp.opportunity_type,
                        )

                logger.info("Scanned %d markets, found %d opportunities", len(all_markets), opps_found)

                # Allow re-evaluation every hour (prices change)
                if len(self._seen_markets) > 2000:
                    self._seen_markets.clear()

            except Exception as exc:
                logger.error("Market scan error: %s", exc, exc_info=True)

            await asyncio.sleep(config.SCAN_INTERVAL_SECONDS)

    async def _fetch_all_markets(self) -> list[MarketInfo]:
        all_markets: list[MarketInfo] = []
        cursor: Optional[str] = None
        for _ in range(10):
            markets = self._client.get_markets(status="open", limit=200, cursor=cursor)
            if not markets:
                break
            all_markets.extend(markets)
            if len(markets) < 200:
                break
            cursor = markets[-1].ticker
        return all_markets

    def _evaluate_market(self, market: MarketInfo) -> Optional[MarketOpportunity]:
        """Check if a market qualifies as a high-edge longshot or favorite."""
        if not market.active or market.resolved:
            return None
        if market.volume_usdc < config.MIN_MARKET_VOLUME:
            return None
        if market.end_date_ts > 0:
            if (market.end_date_ts - time.time()) < config.MIN_TIME_REMAINING_SECONDS:
                return None

        yes = market.yes_price

        # Strategy 1: LONGSHOT FADE
        if 0.02 < yes <= config.LONGSHOT_MAX_PRICE:
            opp = self._create_longshot(market)
            if opp.edge >= config.LONGSHOT_MIN_EDGE:
                return opp

        # Strategy 2: FAVORITE LEAN
        if config.FAVORITE_MIN_PRICE <= yes < 0.98:
            opp = self._create_favorite(market)
            if opp.edge >= config.FAVORITE_MIN_EDGE:
                return opp

        return None

    def _create_longshot(self, market: MarketInfo) -> MarketOpportunity:
        """
        Longshot fade with edge threshold.

        Overestimation is strongest for the cheapest contracts:
          - Under 5c: true prob ~50% lower than priced
          - 5c-10c: true prob ~40% lower
          - 10c-12c: true prob ~30% lower

        We need the edge AFTER the ~1.5c fee to be positive.
        """
        yes = market.yes_price
        if yes <= 0.05:
            overestimation = 0.50
        elif yes <= 0.10:
            overestimation = 0.40
        else:
            overestimation = 0.30

        true_yes = yes * (1.0 - overestimation)
        no_price = 1.0 - yes
        true_no = 1.0 - true_yes
        edge = true_no - no_price  # Our profit margin on the NO side

        return MarketOpportunity(
            market_id=market.market_id, ticker=market.ticker,
            title=market.question, category=market.category,
            side="NO", current_price=no_price,
            estimated_true_prob=true_no, edge=round(edge, 4),
            volume=market.volume_usdc, close_time_ts=market.end_date_ts,
            opportunity_type="longshot",
            wallet_weight=min(edge * 8, 1.0),
            wallet_alias=f"longshot/{market.category}",
            wallet_win_rate=true_no,
        )

    def _create_favorite(self, market: MarketInfo) -> MarketOpportunity:
        """
        Favorite lean — stronger favorites get bigger bonus.

        The favorite-longshot bias means high-probability outcomes
        are underpriced. The effect is strongest above 85c.
        """
        yes = market.yes_price
        if yes >= 0.90:
            bonus = 0.04
        elif yes >= 0.85:
            bonus = 0.035
        elif yes >= 0.80:
            bonus = 0.03
        else:
            bonus = 0.025

        true_yes = min(yes + bonus, 0.99)
        edge = true_yes - yes

        return MarketOpportunity(
            market_id=market.market_id, ticker=market.ticker,
            title=market.question, category=market.category,
            side="YES", current_price=yes,
            estimated_true_prob=true_yes, edge=round(edge, 4),
            volume=market.volume_usdc, close_time_ts=market.end_date_ts,
            opportunity_type="favorite",
            wallet_weight=min(edge * 12, 1.0),
            wallet_alias=f"favorite/{market.category}",
            wallet_win_rate=true_yes,
        )

    # --- Paper Mode ---

    async def _run_paper_mode(self) -> None:
        logger.info("[PAPER] MarketScanner running in simulation mode")

        sample_markets = [
            # Longshots (cheap, all categories)
            {"ticker": "SPORTS-MLB-PIT-W", "title": "Will the Pirates win?", "cat": "sports", "yes": 0.07, "vol": 5200},
            {"ticker": "SPORTS-NBA-ORL-UP", "title": "Will the Magic upset?", "cat": "sports", "yes": 0.10, "vol": 8400},
            {"ticker": "ENT-OSCARS-INDIE", "title": "Indie film wins Best Picture?", "cat": "entertainment", "yes": 0.04, "vol": 6100},
            {"ticker": "POL-3P-STATE", "title": "Third party wins a state?", "cat": "politics", "yes": 0.03, "vol": 12000},
            {"ticker": "CRYPTO-BTC-150K", "title": "BTC hits $150k this month?", "cat": "crypto", "yes": 0.05, "vol": 15000},
            {"ticker": "SPORTS-UFC-KO-R1", "title": "Fight ends in R1 KO?", "cat": "sports", "yes": 0.11, "vol": 7200},
            {"ticker": "ECON-RECESSION-Q2", "title": "US recession in Q2?", "cat": "economics", "yes": 0.08, "vol": 28000},
            # Favorites (strong, all categories)
            {"ticker": "ECON-FED-HOLD", "title": "Fed holds rates?", "cat": "economics", "yes": 0.88, "vol": 45000},
            {"ticker": "POL-INCUMB-WIN", "title": "Incumbent wins?", "cat": "politics", "yes": 0.79, "vol": 22000},
            {"ticker": "ECON-CPI-ABOVE2", "title": "CPI stays above 2%?", "cat": "economics", "yes": 0.92, "vol": 35000},
            {"ticker": "POL-BILL-PASS", "title": "Spending bill passes?", "cat": "politics", "yes": 0.81, "vol": 14000},
            {"ticker": "ECON-JOBS-POS", "title": "Jobs report positive?", "cat": "economics", "yes": 0.86, "vol": 21000},
            {"ticker": "SPORTS-NYY-PO", "title": "Yankees make playoffs?", "cat": "sports", "yes": 0.76, "vol": 18000},
        ]

        while self._running:
            await asyncio.sleep(random.uniform(8, 20))
            if not self._running:
                break

            sample = random.choice(sample_markets)
            noise = random.uniform(-0.015, 0.015)
            yes = max(0.02, min(0.98, sample["yes"] + noise))

            market = MarketInfo(
                market_id=sample["ticker"], ticker=sample["ticker"],
                question=sample["title"], category=sample["cat"],
                yes_price=round(yes, 4), no_price=round(1.0 - yes, 4),
                liquidity_usdc=sample["vol"] * 0.5, volume_usdc=float(sample["vol"]),
                end_date_ts=int(time.time()) + 86400 * random.randint(1, 14),
                active=True, resolved=False,
            )

            opp = self._evaluate_market(market)
            if opp:
                logger.info(
                    "[PAPER] Opportunity: %s %s @ %.2f edge=%.3f vol=$%.0f type=%s",
                    opp.side, opp.ticker, opp.current_price, opp.edge, opp.volume, opp.opportunity_type,
                )
                await self._signal_queue.put(opp)
