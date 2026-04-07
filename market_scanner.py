"""
Market scanner — Latency arbitrage edge detector.

Continuously compares real-time CEX prices (Binance/Coinbase) against
Polymarket's implied contract prices. When the divergence exceeds the
edge threshold (default 3%), emits a trade signal.

This is NOT a periodic scanner — it runs as a continuous async loop
checking for edge on every price tick.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import config
from price_feed import PriceFeed, PriceState

logger = logging.getLogger(__name__)


@dataclass
class MarketOpportunity:
    """A latency arbitrage opportunity."""
    market_id: str
    ticker: str
    title: str
    category: str
    side: str                      # "YES" or "NO"
    current_price: float           # Polymarket contract price we'd pay
    estimated_true_prob: float     # What CEX price implies the true prob is
    edge: float                    # Divergence as a decimal
    volume: float
    close_time_ts: int
    opportunity_type: str          # always "latency_arb"
    timestamp: float = field(default_factory=time.time)

    # CEX data
    cex_price: float = 0.0        # Spot price from Binance/Coinbase
    asset: str = ""               # BTC, ETH
    contract_strike: float = 0.0  # The price level the contract asks about
    latency_ms: float = 0.0       # Estimated lag in Polymarket's price

    # Compatibility fields for position_sizer.py and notifier.py
    wallet_weight: float = 1.0
    wallet_alias: str = ""
    wallet_win_rate: float = 0.0
    wallet_portfolio_pct: float = 0.0


@dataclass
class PolymarketContract:
    """A Polymarket short-duration crypto contract."""
    ticker: str
    condition_id: str
    asset: str           # BTC, ETH
    direction: str       # "up" or "down"
    strike: float        # price level
    duration_minutes: int
    yes_price: float
    no_price: float
    volume: float
    close_time_ts: int
    question: str


class MarketScanner:
    """Latency arbitrage scanner — compares CEX prices to Polymarket contracts."""

    def __init__(self, price_feed: PriceFeed) -> None:
        self._feed = price_feed
        self._signal_queue: asyncio.Queue[MarketOpportunity] = asyncio.Queue()
        self._running = False
        # Active Polymarket contracts to monitor
        self._contracts: list[PolymarketContract] = []
        self._last_contract_refresh: float = 0.0
        self._recent_signals: set[str] = set()  # Prevent duplicate signals

    @property
    def signal_queue(self) -> asyncio.Queue[MarketOpportunity]:
        return self._signal_queue

    async def start(self) -> None:
        self._running = True
        logger.info(
            "MarketScanner starting — LATENCY ARBITRAGE mode "
            "(threshold=%.1f%%, assets=%s, durations=%s)",
            config.EDGE_THRESHOLD_PCT * 100,
            config.TARGET_ASSETS,
            config.TARGET_DURATIONS,
        )

        if config.PAPER_MODE:
            await self._run_paper_arb()
        else:
            await self._run_live_arb()

    async def stop(self) -> None:
        self._running = False
        logger.info("MarketScanner stopped")

    # --- Live Arbitrage Loop ---

    async def _run_live_arb(self) -> None:
        """Continuous edge detection loop."""
        while self._running:
            # Refresh available contracts every 60 seconds
            if time.time() - self._last_contract_refresh > 60:
                await self._refresh_contracts()

            # Check edge on every contract against current CEX price
            for contract in self._contracts:
                price_state = self._feed.get_price(contract.asset)
                if not price_state or price_state.confidence < 0.5:
                    continue

                opp = self._check_edge(contract, price_state)
                if opp:
                    sig_key = f"{opp.ticker}-{opp.side}-{int(opp.timestamp)}"
                    if sig_key not in self._recent_signals:
                        self._recent_signals.add(sig_key)
                        await self._signal_queue.put(opp)
                        logger.info(
                            "EDGE DETECTED: %s %s edge=%.2f%% cex=$%.2f contract_implied=$%.2f",
                            opp.side, opp.ticker, opp.edge * 100,
                            opp.cex_price, opp.contract_strike,
                        )

            # Clear old signals every 5 min
            if len(self._recent_signals) > 10000:
                self._recent_signals.clear()

            # Check every 100ms — fast enough to catch 2.7s windows
            await asyncio.sleep(0.1)

    def _check_edge(
        self, contract: PolymarketContract, cex: PriceState
    ) -> Optional[MarketOpportunity]:
        """
        Compare CEX spot price to Polymarket contract implied price.

        Example: BTC is at $68,500 on Binance. Polymarket has a 15-minute
        contract "Will BTC be above $68,400 in 15 minutes?" with YES at 55c.

        If BTC is already $100 above the strike, the true probability of
        YES is much higher than 55% — more like 75-85% depending on
        volatility. That's a 20-30% edge.
        """
        spot = cex.consensus_price
        strike = contract.strike

        if spot <= 0 or strike <= 0:
            return None

        # Calculate how far spot is from the strike as a percentage
        distance_pct = (spot - strike) / strike

        # Estimate true probability based on distance from strike
        # Closer to expiry + further from strike = higher certainty
        time_left = max(contract.close_time_ts - time.time(), 60)
        minutes_left = time_left / 60

        if contract.direction == "up":
            # "Will BTC be above $X?" — YES is correct if spot > strike
            if distance_pct > 0:
                # Spot is ABOVE strike — YES should be worth more
                # Further above + less time = higher true prob
                true_prob = self._estimate_prob_above(distance_pct, minutes_left, contract.asset)
                market_prob = contract.yes_price
                edge = true_prob - market_prob

                if edge >= config.EDGE_THRESHOLD_PCT and edge <= config.MAX_EDGE_PCT:
                    return self._create_opportunity(contract, cex, "YES", market_prob, true_prob, edge)

            else:
                # Spot is BELOW strike — NO should be worth more
                true_prob_no = self._estimate_prob_above(-distance_pct, minutes_left, contract.asset)
                market_prob_no = contract.no_price
                edge = true_prob_no - market_prob_no

                if edge >= config.EDGE_THRESHOLD_PCT and edge <= config.MAX_EDGE_PCT:
                    return self._create_opportunity(contract, cex, "NO", market_prob_no, true_prob_no, edge)

        return None

    def _estimate_prob_above(self, distance_pct: float, minutes_left: float, asset: str) -> float:
        """
        Estimate true probability that price stays above/below strike.

        Uses a simplified model based on:
          - Distance from strike (bigger = more certain)
          - Time to expiry (less time = more certain if already past strike)
          - Asset volatility (BTC ~0.5% per 15 min, ETH ~0.7%)
        """
        # Annualized vol: BTC ~60%, ETH ~80%. Per-minute vol:
        vol_per_min = {"BTC": 0.0004, "ETH": 0.0006}.get(asset, 0.0005)

        # Expected move in remaining time (1 std dev)
        expected_move = vol_per_min * (minutes_left ** 0.5)

        if expected_move <= 0:
            return 0.95 if distance_pct > 0 else 0.05

        # How many standard deviations is the current price from strike?
        z_score = distance_pct / expected_move

        # Convert z-score to probability using a fast approximation
        # P(staying above) ≈ cumulative normal distribution
        if z_score > 3:
            return 0.98
        elif z_score > 2:
            return 0.95
        elif z_score > 1.5:
            return 0.90
        elif z_score > 1:
            return 0.82
        elif z_score > 0.5:
            return 0.70
        elif z_score > 0.2:
            return 0.58
        else:
            return 0.52

    def _create_opportunity(
        self, contract: PolymarketContract, cex: PriceState,
        side: str, market_price: float, true_prob: float, edge: float,
    ) -> MarketOpportunity:
        return MarketOpportunity(
            market_id=contract.ticker,
            ticker=contract.ticker,
            title=contract.question,
            category="crypto",
            side=side,
            current_price=market_price,
            estimated_true_prob=true_prob,
            edge=round(edge, 4),
            volume=contract.volume,
            close_time_ts=contract.close_time_ts,
            opportunity_type="latency_arb",
            cex_price=cex.consensus_price,
            asset=contract.asset,
            contract_strike=contract.strike,
            latency_ms=round((time.time() - cex.last_updated) * 1000, 1),
            wallet_weight=min(edge * 5, 1.0),
            wallet_alias=f"arb/{contract.asset}",
            wallet_win_rate=true_prob,
            wallet_portfolio_pct=min(edge, 0.10),
        )

    async def _refresh_contracts(self) -> None:
        """Fetch active short-duration crypto contracts from Kalshi."""
        self._last_contract_refresh = time.time()
        try:
            # Import kalshi client to fetch real contracts
            from kalshi_client import KalshiClient
            kalshi = KalshiClient()
            if kalshi.is_connected:
                markets = kalshi.get_crypto_markets()
                self._contracts = []
                for m in markets:
                    if not m.active or m.settled:
                        continue
                    self._contracts.append(PolymarketContract(
                        ticker=m.ticker,
                        condition_id=m.ticker,
                        asset=m.asset,
                        direction="up" if m.direction == "above" else "down",
                        strike=m.strike,
                        duration_minutes=max(1, int((m.close_time_ts - time.time()) / 60)),
                        yes_price=m.yes_price,
                        no_price=m.no_price,
                        volume=m.volume,
                        close_time_ts=m.close_time_ts,
                        question=m.title,
                    ))
                logger.info("Refreshed %d Kalshi crypto contracts", len(self._contracts))
                kalshi.close()
            else:
                logger.warning("Kalshi not connected — no contracts to scan")
        except Exception as exc:
            logger.error("Failed to refresh contracts: %s", exc)

    # --- Paper Mode ---

    async def _run_paper_arb(self) -> None:
        """Simulate latency arbitrage using the paper price feed."""
        logger.info("[PAPER] MarketScanner running in LATENCY ARB simulation mode")

        import random

        # Simulate Polymarket contracts that lag behind CEX
        while self._running:
            await asyncio.sleep(random.uniform(0.5, 3.0))
            if not self._running:
                break

            for asset in config.TARGET_ASSETS:
                cex = self._feed.get_price(asset)
                if not cex or cex.consensus_price <= 0:
                    continue

                spot = cex.consensus_price

                # Simulate a Polymarket contract with a strike near current price
                strike_offset = random.choice([-200, -100, -50, 0, 50, 100, 200])
                if asset == "ETH":
                    strike_offset = strike_offset // 10
                strike = round(spot + strike_offset, 0)

                # Simulate Polymarket's lagging price (2-8 second delay)
                lag_seconds = random.uniform(2, 8)
                distance_pct = (spot - strike) / strike

                # Polymarket's YES price is based on old data (lagging)
                # So it hasn't caught up to the current CEX price
                vol_per_min = 0.0004 if asset == "BTC" else 0.0006
                stale_z = (distance_pct - random.gauss(0, vol_per_min * 2)) / max(vol_per_min * 4, 0.001)

                if stale_z > 0.5:
                    poly_yes = max(0.05, min(0.95, 0.50 + stale_z * 0.08))
                else:
                    poly_yes = max(0.05, min(0.95, 0.50 + stale_z * 0.10))

                minutes_left = random.choice([5, 8, 10, 12, 15, 30, 45, 60])
                close_ts = int(time.time()) + minutes_left * 60

                contract = PolymarketContract(
                    ticker=f"{asset}-UP-{int(strike)}-{minutes_left}m",
                    condition_id=f"0xpaper{int(time.time())}",
                    asset=asset,
                    direction="up",
                    strike=strike,
                    duration_minutes=minutes_left,
                    yes_price=round(poly_yes, 4),
                    no_price=round(1.0 - poly_yes, 4),
                    volume=random.uniform(5000, 80000),
                    close_time_ts=close_ts,
                    question=f"Will {asset} be above ${strike:,.0f} in {minutes_left}m?",
                )

                opp = self._check_edge(contract, cex)
                if opp:
                    logger.info(
                        "[PAPER] ARB SIGNAL: %s %s edge=%.1f%% spot=$%.2f strike=$%.0f poly_yes=%.2f",
                        opp.side, opp.ticker, opp.edge * 100,
                        spot, strike, poly_yes,
                    )
                    await self._signal_queue.put(opp)
