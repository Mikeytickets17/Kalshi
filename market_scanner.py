"""
Market scanner module — aggressive opportunity finder.

Scans ALL Kalshi markets across ALL categories for 5 types of edge:

1. LONGSHOT FADE: YES under 25c in ANY category → buy NO (overpriced longshots)
2. FAVORITE LEAN: YES above 65c in ANY category → buy YES (underpriced favorites)
3. CLOSING CONVERGENCE: Markets closing within 6h where price is drifting
   toward resolution → ride the drift
4. EVENT MULTI-CONTRACT: Multi-outcome events where YES prices don't sum
   correctly → arbitrage the mispricing
5. STALE MIDRANGE: Contracts 25c-65c with very low recent volume relative
   to event volume → price hasn't updated with new info

Scans every 60 seconds (20 seconds for closing-soon markets).
No category restrictions — every market is fair game.
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
    side: str                      # "YES" or "NO"
    current_price: float
    estimated_true_prob: float
    edge: float
    volume: float
    close_time_ts: int
    opportunity_type: str          # longshot/favorite/closing/multi/stale
    timestamp: float = field(default_factory=time.time)

    # Compatibility fields for position_sizer.py and notifier.py
    wallet_weight: float = 1.0
    wallet_alias: str = ""
    wallet_win_rate: float = 0.0
    wallet_portfolio_pct: float = 0.0


class MarketScanner:
    """Aggressive multi-strategy Kalshi market scanner."""

    def __init__(self, client: KalshiClient) -> None:
        self._client = client
        self._signal_queue: asyncio.Queue[MarketOpportunity] = asyncio.Queue()
        self._running: bool = False
        # Track prices over time to detect drift and staleness
        self._price_history: dict[str, list[tuple[float, float]]] = {}  # ticker → [(timestamp, yes_price)]
        self._seen_this_cycle: set[str] = set()
        # Track event-level data for multi-contract arb
        self._event_markets: dict[str, list[MarketInfo]] = {}  # event_ticker → [markets]

    @property
    def signal_queue(self) -> asyncio.Queue[MarketOpportunity]:
        return self._signal_queue

    async def start(self) -> None:
        self._running = True
        logger.info(
            "MarketScanner starting — AGGRESSIVE MODE "
            "(scan=%ds, fast=%ds, longshot<%.0fc, favorite>%.0fc, min_vol=$%d)",
            config.SCAN_INTERVAL_SECONDS, config.FAST_SCAN_INTERVAL_SECONDS,
            config.LONGSHOT_MAX_PRICE * 100, config.FAVORITE_MIN_PRICE * 100,
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
        """Full scan loop — pulls all markets, runs all 5 strategies."""
        while self._running:
            try:
                # Fetch ALL open markets (paginate if needed)
                all_markets = await self._fetch_all_markets()
                logger.info("Scanned %d open markets", len(all_markets))

                self._seen_this_cycle.clear()
                self._event_markets.clear()

                # Group markets by event for multi-contract detection
                for m in all_markets:
                    event_key = self._extract_event_key(m.ticker)
                    if event_key not in self._event_markets:
                        self._event_markets[event_key] = []
                    self._event_markets[event_key].append(m)

                    # Update price history
                    self._record_price(m.ticker, m.yes_price)

                # Run all 5 strategies
                opportunities: list[MarketOpportunity] = []
                for m in all_markets:
                    if not m.active or m.resolved:
                        continue
                    if m.volume_usdc < config.MIN_MARKET_VOLUME:
                        continue

                    opps = self._scan_all_strategies(m)
                    opportunities.extend(opps)

                # Multi-contract arb (needs event-level view)
                for event_key, event_group in self._event_markets.items():
                    if len(event_group) >= 2:
                        arb_opps = self._scan_multi_contract(event_key, event_group)
                        opportunities.extend(arb_opps)

                # Deduplicate and emit
                for opp in opportunities:
                    if opp.market_id not in self._seen_this_cycle:
                        self._seen_this_cycle.add(opp.market_id)
                        await self._signal_queue.put(opp)

                logger.info("Found %d opportunities this cycle", len(opportunities))

            except Exception as exc:
                logger.error("Market scan error: %s", exc, exc_info=True)

            await asyncio.sleep(config.SCAN_INTERVAL_SECONDS)

    async def _fetch_all_markets(self) -> list[MarketInfo]:
        """Fetch all open markets, paginating through the API."""
        all_markets: list[MarketInfo] = []
        cursor: Optional[str] = None
        for _ in range(10):  # Safety: max 10 pages × 200 = 2000 markets
            markets = self._client.get_markets(status="open", limit=200, cursor=cursor)
            if not markets:
                break
            all_markets.extend(markets)
            if len(markets) < 200:
                break
            # Use last ticker as cursor for next page
            cursor = markets[-1].ticker
        return all_markets

    def _scan_all_strategies(self, market: MarketInfo) -> list[MarketOpportunity]:
        """Run all single-market strategies against one market."""
        opps: list[MarketOpportunity] = []

        time_remaining = (market.end_date_ts - time.time()) if market.end_date_ts > 0 else 999999
        if time_remaining < config.MIN_TIME_REMAINING_SECONDS:
            return opps

        yes = market.yes_price

        # Strategy 1: LONGSHOT FADE — any category, YES under 25c
        if 0.01 < yes <= config.LONGSHOT_MAX_PRICE:
            opps.append(self._create_longshot(market))

        # Strategy 2: FAVORITE LEAN — any category, YES above 65c
        if config.FAVORITE_MIN_PRICE <= yes < 0.99:
            opps.append(self._create_favorite(market))

        # Strategy 3: CLOSING CONVERGENCE — within 6h of close, price drifting
        closing_threshold = config.CLOSING_SOON_HOURS * 3600
        if 0 < time_remaining < closing_threshold:
            drift_opp = self._scan_closing_drift(market, time_remaining)
            if drift_opp:
                opps.append(drift_opp)

        # Strategy 4: STALE MIDRANGE — price in 25c-65c with low volume (stale price)
        if config.MIDRANGE_LOW <= yes <= config.MIDRANGE_HIGH:
            stale_opp = self._scan_stale_midrange(market)
            if stale_opp:
                opps.append(stale_opp)

        return opps

    # --- Strategy 1: Longshot Fade ---

    def _create_longshot(self, market: MarketInfo) -> MarketOpportunity:
        """Longshot bias: YES under 25c is overpriced. Buy NO."""
        yes = market.yes_price
        # Overestimation scales with how cheap the longshot is
        # Under 5c: ~50% overestimated. 5-15c: ~40%. 15-25c: ~25%.
        if yes <= 0.05:
            overestimation = 0.50
        elif yes <= 0.15:
            overestimation = 0.40
        else:
            overestimation = 0.25

        true_yes = yes * (1.0 - overestimation)
        no_price = 1.0 - yes
        true_no = 1.0 - true_yes
        edge = true_no - no_price

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

    # --- Strategy 2: Favorite Lean ---

    def _create_favorite(self, market: MarketInfo) -> MarketOpportunity:
        """Favorite bias: YES above 65c is underpriced. Buy YES."""
        yes = market.yes_price
        # Underestimation scales with how strong the favorite is
        # 65-80c: +2.5%. 80-90c: +3%. 90-99c: +3.5%.
        if yes >= 0.90:
            bonus = 0.035
        elif yes >= 0.80:
            bonus = 0.030
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

    # --- Strategy 3: Closing Convergence ---

    def _scan_closing_drift(self, market: MarketInfo, time_remaining: float) -> Optional[MarketOpportunity]:
        """
        Markets closing soon with a strong price trend.

        When a market is about to close, prices converge toward the outcome.
        If price has been drifting strongly in one direction over the last
        few scans, ride that momentum — it usually means informed traders
        are pushing toward the correct resolution.
        """
        history = self._price_history.get(market.ticker, [])
        if len(history) < 3:
            return None  # Need at least 3 data points

        # Calculate drift over recent history
        recent = history[-5:]  # Last 5 readings
        if len(recent) < 3:
            return None

        first_price = recent[0][1]
        last_price = recent[-1][1]
        drift = last_price - first_price

        # Minimum drift to act on: 3 cents in either direction
        if abs(drift) < 0.03:
            return None

        # Urgency multiplier: closer to close = stronger signal
        hours_left = time_remaining / 3600
        urgency = max(0.5, min(2.0, 3.0 / max(hours_left, 0.5)))

        if drift > 0:
            # Price drifting UP → YES is becoming more likely
            side = "YES"
            price = last_price
            edge = abs(drift) * 0.5 * urgency  # Expect drift to continue partway
            true_prob = min(price + edge, 0.98)
        else:
            # Price drifting DOWN → NO is becoming more likely
            side = "NO"
            price = 1.0 - last_price
            edge = abs(drift) * 0.5 * urgency
            true_prob = min(price + edge, 0.98)

        if edge < 0.015:
            return None

        return MarketOpportunity(
            market_id=market.market_id + "-closing", ticker=market.ticker,
            title=market.question, category=market.category,
            side=side, current_price=price,
            estimated_true_prob=true_prob, edge=round(edge, 4),
            volume=market.volume_usdc, close_time_ts=market.end_date_ts,
            opportunity_type="closing",
            wallet_weight=min(edge * 10 * urgency, 1.0),
            wallet_alias=f"closing/{hours_left:.1f}h",
            wallet_win_rate=true_prob,
        )

    # --- Strategy 4: Stale Midrange ---

    def _scan_stale_midrange(self, market: MarketInfo) -> Optional[MarketOpportunity]:
        """
        Midrange contracts (25c-65c) with low volume may have stale prices.

        If the market's volume is low relative to similar events, the price
        may not reflect current information. We lean toward the direction
        of recent drift or toward 50/50 (regression to mean in uncertain markets).
        """
        history = self._price_history.get(market.ticker, [])

        # Only flag truly stale markets: volume under $2000 and in 30-60c range
        if market.volume_usdc > 2000:
            return None
        if not (0.30 <= market.yes_price <= 0.60):
            return None

        # With very little history, lean toward the side closest to 50c
        # (uncertain markets tend to drift to 50/50, which means buying
        # the cheaper side has a small statistical edge)
        yes = market.yes_price
        if yes < 0.45:
            # YES is cheap relative to 50/50 → slight YES lean
            side = "YES"
            price = yes
            edge = (0.50 - yes) * 0.15  # Small edge: 15% of distance to 50c
            true_prob = yes + edge
        elif yes > 0.55:
            # NO is cheap relative to 50/50 → slight NO lean
            side = "NO"
            price = 1.0 - yes
            edge = (yes - 0.50) * 0.15
            true_prob = price + edge
        else:
            return None  # Too close to 50/50, no edge

        if edge < 0.005:
            return None

        return MarketOpportunity(
            market_id=market.market_id + "-stale", ticker=market.ticker,
            title=market.question, category=market.category,
            side=side, current_price=price,
            estimated_true_prob=true_prob, edge=round(edge, 4),
            volume=market.volume_usdc, close_time_ts=market.end_date_ts,
            opportunity_type="stale",
            wallet_weight=min(edge * 15, 0.6),  # Lower weight — less confident
            wallet_alias=f"stale/{market.category}",
            wallet_win_rate=true_prob,
        )

    # --- Strategy 5: Multi-Contract Arbitrage ---

    def _scan_multi_contract(self, event_key: str, markets: list[MarketInfo]) -> list[MarketOpportunity]:
        """
        Multi-outcome events where YES prices don't sum to 100%.

        Example: "Who will win the division?" with 5 teams. If the YES prices
        for all 5 teams sum to 110c instead of 100c, every team is overpriced.
        Buy NO on all of them (or the most overpriced ones).

        Conversely, if they sum to 90c, they're all underpriced — buy YES
        on the most likely outcomes.
        """
        opps: list[MarketOpportunity] = []

        active = [m for m in markets if m.active and not m.resolved and m.yes_price > 0.01]
        if len(active) < 2:
            return opps

        total_yes = sum(m.yes_price for m in active)

        # Should sum to ~1.00. Deviation = overpricing or underpricing.
        deviation = total_yes - 1.0

        if abs(deviation) < 0.03:
            return opps  # Within 3c of fair, not enough edge

        if deviation > 0:
            # Overpriced: total > 100%. Sell NO on the most overpriced contracts.
            # The most overpriced longshots get the biggest correction.
            fair_share = deviation / len(active)
            for m in active:
                if m.yes_price <= 0.30:  # Only fade the longshots in overpriced events
                    correction = m.yes_price * (deviation / total_yes)
                    true_yes = m.yes_price - correction
                    no_price = 1.0 - m.yes_price
                    true_no = 1.0 - true_yes
                    edge = true_no - no_price

                    if edge > 0.01:
                        opps.append(MarketOpportunity(
                            market_id=m.market_id + "-arb", ticker=m.ticker,
                            title=m.question, category=m.category,
                            side="NO", current_price=no_price,
                            estimated_true_prob=true_no, edge=round(edge, 4),
                            volume=m.volume_usdc, close_time_ts=m.end_date_ts,
                            opportunity_type="multi_arb",
                            wallet_weight=min(edge * 10, 1.0),
                            wallet_alias=f"arb/{event_key[:15]}",
                            wallet_win_rate=true_no,
                        ))
        else:
            # Underpriced: total < 100%. Buy YES on the favorites.
            for m in active:
                if m.yes_price >= 0.50:
                    correction = m.yes_price * (abs(deviation) / total_yes)
                    true_yes = m.yes_price + correction
                    edge = true_yes - m.yes_price

                    if edge > 0.01:
                        opps.append(MarketOpportunity(
                            market_id=m.market_id + "-arb", ticker=m.ticker,
                            title=m.question, category=m.category,
                            side="YES", current_price=m.yes_price,
                            estimated_true_prob=min(true_yes, 0.99), edge=round(edge, 4),
                            volume=m.volume_usdc, close_time_ts=m.end_date_ts,
                            opportunity_type="multi_arb",
                            wallet_weight=min(edge * 10, 1.0),
                            wallet_alias=f"arb/{event_key[:15]}",
                            wallet_win_rate=min(true_yes, 0.99),
                        ))

        return opps

    # --- Helpers ---

    def _extract_event_key(self, ticker: str) -> str:
        """Extract the event part of a ticker for grouping related markets."""
        # Kalshi tickers are like "TEAM-MLB-NYY-2024-04-06" or "PRES-2024-DEM"
        # Group by removing the last segment (usually the specific outcome)
        parts = ticker.rsplit("-", 1)
        return parts[0] if len(parts) > 1 else ticker

    def _record_price(self, ticker: str, yes_price: float) -> None:
        """Record a price observation for drift detection."""
        now = time.time()
        if ticker not in self._price_history:
            self._price_history[ticker] = []
        self._price_history[ticker].append((now, yes_price))
        # Keep last 20 observations per market
        self._price_history[ticker] = self._price_history[ticker][-20:]

    # --- Paper Mode Simulation ---

    async def _run_paper_mode(self) -> None:
        """Simulate aggressive scanning in paper mode."""
        logger.info("[PAPER] MarketScanner running in AGGRESSIVE simulation mode")

        sample_markets = [
            # Longshots — all categories
            {"ticker": "SPORTS-MLB-PIT-W", "title": "Will the Pirates win tonight?", "cat": "sports", "yes": 0.08, "vol": 5200},
            {"ticker": "SPORTS-NBA-ORL-UPSET", "title": "Will the Magic upset the Celtics?", "cat": "sports", "yes": 0.14, "vol": 8400},
            {"ticker": "SPORTS-NHL-SJ-W", "title": "Will the Sharks win?", "cat": "sports", "yes": 0.06, "vol": 3100},
            {"ticker": "ENT-OSCARS-INDIE", "title": "Will an indie film win Best Picture?", "cat": "entertainment", "yes": 0.05, "vol": 4500},
            {"ticker": "POL-3P-CANDIDATE", "title": "Will a third party win a state?", "cat": "politics", "yes": 0.03, "vol": 12000},
            {"ticker": "ECON-RECESSION-Q2", "title": "Will US enter recession in Q2?", "cat": "economics", "yes": 0.12, "vol": 28000},
            {"ticker": "CRYPTO-BTC-100K", "title": "Will BTC hit $100k this week?", "cat": "crypto", "yes": 0.09, "vol": 15000},
            {"ticker": "WEATHER-CAT5-APR", "title": "Will a Cat 5 hurricane hit in April?", "cat": "weather", "yes": 0.02, "vol": 6000},
            {"ticker": "SPORTS-UFC-KO-R1", "title": "Will the fight end in R1 KO?", "cat": "sports", "yes": 0.18, "vol": 7200},
            {"ticker": "SPORTS-NFL-DRAFT-DEF", "title": "Will a defensive player go #1?", "cat": "sports", "yes": 0.11, "vol": 9800},
            # Favorites — all categories
            {"ticker": "ECON-FED-HOLD-APR", "title": "Will the Fed hold rates in April?", "cat": "economics", "yes": 0.82, "vol": 45000},
            {"ticker": "POL-INCUMB-WIN", "title": "Will the incumbent win?", "cat": "politics", "yes": 0.75, "vol": 22000},
            {"ticker": "ECON-CPI-ABOVE-2", "title": "Will CPI stay above 2%?", "cat": "economics", "yes": 0.91, "vol": 35000},
            {"ticker": "SPORTS-NYY-MAKE-PO", "title": "Will the Yankees make the playoffs?", "cat": "sports", "yes": 0.68, "vol": 18000},
            {"ticker": "POL-BILL-PASS", "title": "Will the spending bill pass?", "cat": "politics", "yes": 0.78, "vol": 14000},
            {"ticker": "ECON-JOBS-POSITIVE", "title": "Will jobs report be positive?", "cat": "economics", "yes": 0.88, "vol": 21000},
            {"ticker": "CRYPTO-ETH-ABOVE-3K", "title": "Will ETH stay above $3k?", "cat": "crypto", "yes": 0.72, "vol": 11000},
            # Midrange (potential stale)
            {"ticker": "POL-DEBATE-WINNER", "title": "Who won the debate?", "cat": "politics", "yes": 0.42, "vol": 800},
            {"ticker": "SPORTS-MVP-JOKIC", "title": "Will Jokic win MVP?", "cat": "sports", "yes": 0.38, "vol": 1200},
            {"ticker": "ENT-ALBUM-PLAT", "title": "Will the album go platinum?", "cat": "entertainment", "yes": 0.55, "vol": 600},
            # Multi-contract events (same event, different outcomes)
            {"ticker": "DIV-NLE-ATL", "title": "Will the Braves win the NL East?", "cat": "sports", "yes": 0.28, "vol": 4000, "event": "DIV-NLE"},
            {"ticker": "DIV-NLE-NYM", "title": "Will the Mets win the NL East?", "cat": "sports", "yes": 0.25, "vol": 3800, "event": "DIV-NLE"},
            {"ticker": "DIV-NLE-PHI", "title": "Will the Phillies win the NL East?", "cat": "sports", "yes": 0.35, "vol": 5200, "event": "DIV-NLE"},
            {"ticker": "DIV-NLE-MIA", "title": "Will the Marlins win the NL East?", "cat": "sports", "yes": 0.08, "vol": 2100, "event": "DIV-NLE"},
            {"ticker": "DIV-NLE-WSH", "title": "Will the Nationals win the NL East?", "cat": "sports", "yes": 0.06, "vol": 1800, "event": "DIV-NLE"},
        ]

        scan_count = 0
        while self._running:
            await asyncio.sleep(random.uniform(3, 8))
            if not self._running:
                break

            scan_count += 1
            batch_size = random.randint(3, 8)
            batch = random.sample(sample_markets, min(batch_size, len(sample_markets)))

            all_market_infos: list[MarketInfo] = []
            for s in batch:
                noise = random.uniform(-0.02, 0.02)
                yes = max(0.02, min(0.98, s["yes"] + noise))
                hours_left = random.uniform(0.5, 72)
                mi = MarketInfo(
                    market_id=s["ticker"], ticker=s["ticker"],
                    question=s["title"], category=s["cat"],
                    yes_price=round(yes, 4), no_price=round(1.0 - yes, 4),
                    liquidity_usdc=s["vol"] * 0.5, volume_usdc=float(s["vol"]),
                    end_date_ts=int(time.time()) + int(hours_left * 3600),
                    active=True, resolved=False,
                )
                all_market_infos.append(mi)
                self._record_price(mi.ticker, mi.yes_price)

            # Group for multi-contract
            self._event_markets.clear()
            for mi in all_market_infos:
                ek = self._extract_event_key(mi.ticker)
                if ek not in self._event_markets:
                    self._event_markets[ek] = []
                self._event_markets[ek].append(mi)

            # Run all strategies
            opps: list[MarketOpportunity] = []
            for mi in all_market_infos:
                opps.extend(self._scan_all_strategies(mi))

            for ek, eg in self._event_markets.items():
                if len(eg) >= 2:
                    opps.extend(self._scan_multi_contract(ek, eg))

            for opp in opps:
                logger.info(
                    "[PAPER] Signal: %s %s @ %.2f edge=%.3f type=%s",
                    opp.side, opp.ticker, opp.current_price, opp.edge, opp.opportunity_type,
                )
                await self._signal_queue.put(opp)

            if scan_count % 10 == 0:
                logger.info("[PAPER] Scan cycle %d — %d signals emitted", scan_count, len(opps))
