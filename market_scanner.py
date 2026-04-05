"""
Market scanner module (replaces wallet_tracker.py).

Scans Kalshi API for open markets matching longshot bias criteria:
  - YES contracts priced under LONGSHOT_MAX_PRICE in sports/entertainment
  - YES contracts priced above FAVORITE_MIN_PRICE in economics/politics

Emits MarketOpportunity objects for the signal evaluator.
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
    """
    A market opportunity detected by the scanner.

    Contains compatibility fields (wallet_weight, wallet_alias, wallet_win_rate,
    wallet_portfolio_pct) so that position_sizer.py and notifier.py work without
    modification — they were built for the copy-trading strategy and read these.
    """
    market_id: str
    ticker: str
    title: str
    category: str
    side: str                      # "YES" or "NO" — which side WE should buy
    current_price: float           # current price of the side we'd buy
    estimated_true_prob: float     # our estimate of the true probability
    edge: float                    # estimated_true_prob - current_price (or inverse)
    volume: float                  # market volume in USD
    close_time_ts: int             # market close timestamp
    opportunity_type: str          # "longshot" or "favorite"
    timestamp: float = field(default_factory=time.time)

    # Compatibility fields for position_sizer.py and notifier.py
    wallet_weight: float = 1.0     # position_sizer uses this as a multiplier
    wallet_alias: str = ""         # notifier displays this as the signal source
    wallet_win_rate: float = 0.0   # notifier displays this
    wallet_portfolio_pct: float = 0.0  # position_sizer checks conviction threshold


class MarketScanner:
    """Scans Kalshi for longshot and favorite bias opportunities."""

    def __init__(self, client: KalshiClient) -> None:
        self._client = client
        self._signal_queue: asyncio.Queue[MarketOpportunity] = asyncio.Queue()
        self._running: bool = False
        self._seen_markets: set[str] = set()
        self._scan_interval = config.SCAN_INTERVAL_SECONDS

    @property
    def signal_queue(self) -> asyncio.Queue[MarketOpportunity]:
        """Queue of detected market opportunities."""
        return self._signal_queue

    async def start(self) -> None:
        """Start the market scanner loop."""
        self._running = True
        logger.info(
            "MarketScanner starting (interval=%ds, longshot_max=%.2f, favorite_min=%.2f)",
            self._scan_interval, config.LONGSHOT_MAX_PRICE, config.FAVORITE_MIN_PRICE,
        )

        if config.PAPER_MODE and not self._client.is_connected:
            await self._run_paper_mode()
        else:
            await self._run_live_scan()

    async def stop(self) -> None:
        """Stop the market scanner."""
        self._running = False
        logger.info("MarketScanner stopped")

    # --- Live Scanning ---

    async def _run_live_scan(self) -> None:
        """Periodically scan Kalshi for opportunities."""
        while self._running:
            try:
                markets = self._client.get_markets(status="open", limit=200)
                logger.info("Scanned %d open markets", len(markets))

                for market in markets:
                    opp = self._evaluate_market(market)
                    if opp and opp.market_id not in self._seen_markets:
                        self._seen_markets.add(opp.market_id)
                        await self._signal_queue.put(opp)
                        logger.info(
                            "Opportunity found: %s %s @ %.2f (edge=%.3f, type=%s)",
                            opp.side, opp.ticker, opp.current_price,
                            opp.edge, opp.opportunity_type,
                        )

                # Prune old seen markets (allow re-evaluation if prices change)
                if len(self._seen_markets) > 5000:
                    self._seen_markets.clear()

            except Exception as exc:
                logger.error("Market scan error: %s", exc, exc_info=True)

            await asyncio.sleep(self._scan_interval)

    def _evaluate_market(self, market: MarketInfo) -> Optional[MarketOpportunity]:
        """Check if a market matches our longshot or favorite criteria."""
        if not market.active or market.resolved:
            return None

        # Volume filter
        if market.volume_usdc < config.MIN_MARKET_VOLUME:
            return None

        # Time remaining filter
        if market.end_date_ts > 0:
            time_remaining = market.end_date_ts - time.time()
            if time_remaining < config.MIN_TIME_REMAINING_SECONDS:
                return None

        category = market.category.lower()

        # Longshot detection: YES contracts priced under 15c in sports/entertainment
        if market.yes_price <= config.LONGSHOT_MAX_PRICE and market.yes_price > 0.01:
            if any(cat in category for cat in config.LONGSHOT_CATEGORIES):
                return self._create_longshot_opportunity(market)

        # Favorite detection: YES contracts priced above 70c in economics/politics
        if market.yes_price >= config.FAVORITE_MIN_PRICE and market.yes_price < 0.99:
            if any(cat in category for cat in config.FAVORITE_CATEGORIES):
                return self._create_favorite_opportunity(market)

        return None

    def _create_longshot_opportunity(self, market: MarketInfo) -> MarketOpportunity:
        """
        Create a longshot opportunity.

        Longshot bias: markets overestimate low-probability events.
        A YES at 10c implies 10% probability, but research suggests the true
        probability is ~40% lower → actually ~6%. We sell NO (buy NO) to profit.

        Our action: buy NO contracts at (1 - yes_price).
        """
        yes_price = market.yes_price
        # Longshot bias adjustment: true prob is ~40% lower than market price
        overestimation_factor = 0.40
        estimated_true_prob = yes_price * (1.0 - overestimation_factor)
        # Our edge: we buy NO at (1 - yes_price), true NO prob is (1 - estimated_true_prob)
        no_price = 1.0 - yes_price
        true_no_prob = 1.0 - estimated_true_prob
        edge = true_no_prob - no_price

        return MarketOpportunity(
            market_id=market.market_id,
            ticker=market.ticker,
            title=market.question,
            category=market.category,
            side="NO",  # We buy NO against the overpriced longshot
            current_price=no_price,
            estimated_true_prob=true_no_prob,
            edge=round(edge, 4),
            volume=market.volume_usdc,
            close_time_ts=market.end_date_ts,
            opportunity_type="longshot",
            wallet_weight=min(edge * 10, 1.0),  # Higher edge → higher sizing weight
            wallet_alias=f"longshot/{market.category}",
            wallet_win_rate=true_no_prob,  # Display our estimated win prob
            wallet_portfolio_pct=0.0,  # No conviction multiplier for scanner
        )

    def _create_favorite_opportunity(self, market: MarketInfo) -> MarketOpportunity:
        """
        Create a favorite opportunity.

        Favorite bias: high-probability outcomes are slightly underpriced.
        A YES at 75c implies 75%, but research suggests true prob is ~2-3% higher → ~77-78%.

        Our action: buy YES contracts at the current price.
        """
        yes_price = market.yes_price
        # Favorite bias adjustment: true prob is 2-3% higher than market price
        underestimation_bonus = 0.025
        estimated_true_prob = min(yes_price + underestimation_bonus, 0.99)
        edge = estimated_true_prob - yes_price

        return MarketOpportunity(
            market_id=market.market_id,
            ticker=market.ticker,
            title=market.question,
            category=market.category,
            side="YES",  # We buy YES on the underpriced favorite
            current_price=yes_price,
            estimated_true_prob=estimated_true_prob,
            edge=round(edge, 4),
            volume=market.volume_usdc,
            close_time_ts=market.end_date_ts,
            opportunity_type="favorite",
            wallet_weight=min(edge * 15, 1.0),  # Scale weight by edge size
            wallet_alias=f"favorite/{market.category}",
            wallet_win_rate=estimated_true_prob,
            wallet_portfolio_pct=0.0,
        )

    # --- Paper Mode Simulation ---

    async def _run_paper_mode(self) -> None:
        """Simulate market scanning in paper mode when no API connection."""
        logger.info("[PAPER] MarketScanner running in simulation mode")

        sample_markets = [
            {"ticker": "SPORTS-MLB-NYY-WIN", "title": "Will the Yankees win tonight?", "category": "sports",
             "yes_price": 0.08, "volume": 5200, "type": "longshot"},
            {"ticker": "SPORTS-NBA-FINALS", "title": "Will underdogs win the NBA Finals?", "category": "sports",
             "yes_price": 0.12, "volume": 8400, "type": "longshot"},
            {"ticker": "ENT-OSCARS-UPSET", "title": "Will an indie film win Best Picture?", "category": "entertainment",
             "yes_price": 0.06, "volume": 3100, "type": "longshot"},
            {"ticker": "ECON-FED-HOLD", "title": "Will the Fed hold rates?", "category": "economics",
             "yes_price": 0.82, "volume": 45000, "type": "favorite"},
            {"ticker": "POL-SENATE-INCUMB", "title": "Will the incumbent win?", "category": "politics",
             "yes_price": 0.75, "volume": 22000, "type": "favorite"},
            {"ticker": "SPORTS-NFL-SPREAD", "title": "Will the underdog cover?", "category": "sports",
             "yes_price": 0.11, "volume": 6800, "type": "longshot"},
            {"ticker": "ECON-CPI-UNDER", "title": "Will CPI come in under forecast?", "category": "economics",
             "yes_price": 0.78, "volume": 15000, "type": "favorite"},
            {"ticker": "ENT-EMMY-DARK", "title": "Will a dark horse win at the Emmys?", "category": "entertainment",
             "yes_price": 0.09, "volume": 2800, "type": "longshot"},
        ]

        while self._running:
            await asyncio.sleep(random.uniform(20, 60))
            if not self._running:
                break

            sample = random.choice(sample_markets)
            # Add some price noise
            price_noise = random.uniform(-0.02, 0.02)
            yes_price = max(0.02, min(0.98, sample["yes_price"] + price_noise))

            market = MarketInfo(
                market_id=sample["ticker"],
                ticker=sample["ticker"],
                question=sample["title"],
                category=sample["category"],
                yes_price=round(yes_price, 4),
                no_price=round(1.0 - yes_price, 4),
                liquidity_usdc=sample["volume"] * 0.5,
                volume_usdc=float(sample["volume"]),
                end_date_ts=int(time.time()) + 86400 * random.randint(1, 14),
                active=True,
                resolved=False,
            )

            opp = self._evaluate_market(market)
            if opp:
                logger.info(
                    "[PAPER] Opportunity: %s %s @ %.2f (edge=%.3f, type=%s)",
                    opp.side, opp.ticker, opp.current_price, opp.edge, opp.opportunity_type,
                )
                await self._signal_queue.put(opp)
