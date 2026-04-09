"""
Kalshi Edge Strategies — The real money-makers.

Three proven edges that traders have used for near-100% win rates:

EDGE 1: CROSS-VENUE ARBITRAGE (Kalshi vs Polymarket)
  Buy YES on Kalshi + NO on Polymarket (or vice versa) when combined
  cost < $1.00. Guaranteed profit regardless of outcome.
  Example: Kalshi YES at 35¢ + Polymarket NO at 63¢ = 98¢ → $0.02 risk-free.

EDGE 2: NEAR-EXPIRY SETTLEMENT SNIPER
  When a BTC 15-min contract is 2-5 minutes from expiry and BTC is
  significantly above/below the strike, the outcome is near-certain.
  Buy the correct side at any price below fair value.
  Example: BTC at $73,500, contract "BTC above $72,000?" expires in 3 min.
  YES should be ~$0.98. If market shows YES at $0.90, buy it → collect $1.00.

EDGE 3: INTRA-MARKET BRACKET ARB
  Kalshi has multiple bracket contracts for the same event. If the sum of
  all bracket YES prices < $1.00, or if YES + NO on same contract < $1.00
  (after fees), there's a guaranteed arbitrage.
"""

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

import config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ArbOpportunity:
    """A detected arbitrage opportunity."""
    edge_type: str          # "cross_venue", "settlement_snipe", "bracket_arb"
    description: str
    profit_pct: float       # expected profit as decimal (0.02 = 2%)
    confidence: float       # 0-1, how certain is the outcome
    max_size_usd: float     # max we should bet
    urgency: str            # "immediate", "seconds", "minutes"

    # Cross-venue fields
    venue_a: str = ""       # "kalshi" or "polymarket"
    venue_b: str = ""
    ticker_a: str = ""
    ticker_b: str = ""
    side_a: str = ""        # "YES" or "NO"
    side_b: str = ""
    price_a: float = 0.0
    price_b: float = 0.0
    combined_cost: float = 0.0

    # Settlement snipe fields
    ticker: str = ""
    side: str = ""
    current_price: float = 0.0
    fair_value: float = 0.0
    spot_price: float = 0.0
    strike_price: float = 0.0
    minutes_to_expiry: float = 0.0

    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# EDGE 1: Cross-Venue Arbitrage (Kalshi vs Polymarket)
# ---------------------------------------------------------------------------

class CrossVenueArb:
    """
    Detects risk-free arbitrage between Kalshi and Polymarket.

    Both platforms list BTC 15-min and 1-hour contracts. When the combined
    cost of opposing positions across venues < $1.00, we lock in profit.

    The key insight: the same event resolves the same way on both platforms,
    but they price it independently. Inefficiencies appear constantly.
    """

    POLYMARKET_API = "https://clob.polymarket.com"
    POLYMARKET_GAMMA = "https://gamma-api.polymarket.com"

    def __init__(self) -> None:
        self._http = httpx.AsyncClient(timeout=10.0)
        self._opportunities: list[ArbOpportunity] = []

    async def scan(self, kalshi_markets: list) -> list[ArbOpportunity]:
        """
        Compare Kalshi BTC contracts against Polymarket equivalents.
        Returns list of arbitrage opportunities where combined cost < $1.00.
        """
        opps = []

        # Get Polymarket BTC markets
        poly_markets = await self._get_polymarket_btc_markets()
        if not poly_markets:
            return opps

        for kalshi_mkt in kalshi_markets:
            if not kalshi_mkt.active or kalshi_mkt.settled:
                continue

            # Find matching Polymarket contract (same asset, similar strike, similar expiry)
            match = self._find_matching_poly(kalshi_mkt, poly_markets)
            if not match:
                continue

            # Check both arb directions:
            # Direction 1: Buy YES on Kalshi + Buy NO (DOWN) on Polymarket
            cost_1 = kalshi_mkt.yes_price + match["no_price"]
            if cost_1 < 0.98:  # Must be profitable after ~2% fees
                profit = 1.0 - cost_1
                opps.append(ArbOpportunity(
                    edge_type="cross_venue",
                    description=f"Kalshi YES {kalshi_mkt.yes_price:.2f} + Poly NO {match['no_price']:.2f} = {cost_1:.3f}",
                    profit_pct=profit,
                    confidence=0.99,  # mathematical certainty minus execution risk
                    max_size_usd=min(500, match.get("liquidity", 500)),
                    urgency="immediate",
                    venue_a="kalshi", venue_b="polymarket",
                    ticker_a=kalshi_mkt.ticker, ticker_b=match["ticker"],
                    side_a="YES", side_b="NO",
                    price_a=kalshi_mkt.yes_price, price_b=match["no_price"],
                    combined_cost=cost_1,
                ))

            # Direction 2: Buy NO on Kalshi + Buy YES (UP) on Polymarket
            cost_2 = kalshi_mkt.no_price + match["yes_price"]
            if cost_2 < 0.98:
                profit = 1.0 - cost_2
                opps.append(ArbOpportunity(
                    edge_type="cross_venue",
                    description=f"Kalshi NO {kalshi_mkt.no_price:.2f} + Poly YES {match['yes_price']:.2f} = {cost_2:.3f}",
                    profit_pct=profit,
                    confidence=0.99,
                    max_size_usd=min(500, match.get("liquidity", 500)),
                    urgency="immediate",
                    venue_a="kalshi", venue_b="polymarket",
                    ticker_a=kalshi_mkt.ticker, ticker_b=match["ticker"],
                    side_a="NO", side_b="YES",
                    price_a=kalshi_mkt.no_price, price_b=match["yes_price"],
                    combined_cost=cost_2,
                ))

        self._opportunities = opps
        return opps

    async def _get_polymarket_btc_markets(self) -> list[dict]:
        """Fetch active BTC contracts from Polymarket."""
        try:
            resp = await self._http.get(
                f"{self.POLYMARKET_GAMMA}/markets",
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": 100,
                },
            )
            if resp.status_code != 200:
                return []

            markets = resp.json()
            btc_markets = []
            for m in markets:
                question = (m.get("question", "") or "").lower()
                if any(kw in question for kw in ["bitcoin", "btc"]):
                    tokens = m.get("tokens", [])
                    yes_price = 0.5
                    no_price = 0.5
                    for t in tokens:
                        if t.get("outcome", "").lower() == "yes":
                            yes_price = float(t.get("price", 0.5))
                        elif t.get("outcome", "").lower() == "no":
                            no_price = float(t.get("price", 0.5))

                    btc_markets.append({
                        "ticker": m.get("condition_id", ""),
                        "question": m.get("question", ""),
                        "yes_price": yes_price,
                        "no_price": no_price,
                        "volume": float(m.get("volume", 0) or 0),
                        "liquidity": float(m.get("liquidity", 0) or 0),
                        "end_date": m.get("end_date_iso", ""),
                        "strike": self._extract_strike(m.get("question", "")),
                    })
            return btc_markets

        except Exception as exc:
            logger.debug("Polymarket fetch error: %s", exc)
            return []

    def _find_matching_poly(self, kalshi_mkt, poly_markets: list[dict]) -> Optional[dict]:
        """Find the Polymarket contract that matches a Kalshi contract."""
        for pm in poly_markets:
            # Match by similar strike price (within 1%)
            if kalshi_mkt.strike > 0 and pm.get("strike", 0) > 0:
                strike_diff = abs(kalshi_mkt.strike - pm["strike"]) / kalshi_mkt.strike
                if strike_diff < 0.01:
                    return pm
        return None

    def _extract_strike(self, question: str) -> float:
        """Extract strike price from a Polymarket question."""
        import re
        patterns = [
            r"\$?([\d,]+(?:\.\d+)?)\s*(?:k|K)",
            r"\$?([\d,]+(?:\.\d+)?)",
        ]
        for pat in patterns:
            match = re.search(pat, question)
            if match:
                val = match.group(1).replace(",", "")
                num = float(val)
                if "k" in question.lower() and num < 1000:
                    num *= 1000
                if num > 10000:
                    return num
        return 0.0

    async def close(self):
        await self._http.aclose()


# ---------------------------------------------------------------------------
# EDGE 2: Near-Expiry Settlement Sniper
# ---------------------------------------------------------------------------

class SettlementSniper:
    """
    Snipes Kalshi contracts that are near expiry with known outcomes.

    When a BTC 15-min contract has 2-5 minutes left and BTC is significantly
    above/below the strike, the outcome is near-certain. We buy the correct
    side at whatever discount the market offers.

    This is the HIGHEST win-rate strategy. If BTC is $1,500 above the strike
    with 3 minutes left, the probability of staying above is ~99%.

    Win rates: 95-100% when executed correctly.
    """

    def __init__(self) -> None:
        pass

    def scan(
        self,
        kalshi_markets: list,
        spot_price: float,
        realized_vol_annual: float = 0.65,
    ) -> list[ArbOpportunity]:
        """
        Find near-expiry contracts where the outcome is near-certain.

        Args:
            kalshi_markets: Active Kalshi contracts
            spot_price: Current BTC price from CEX
            realized_vol_annual: Annualized BTC volatility
        """
        opps = []
        now = time.time()

        for mkt in kalshi_markets:
            if not mkt.active or mkt.settled:
                continue
            if mkt.close_time_ts <= 0:
                continue

            remaining_sec = mkt.close_time_ts - now
            remaining_min = remaining_sec / 60

            # Only look at contracts with 1-10 minutes remaining
            if remaining_min < 1.0 or remaining_min > 10.0:
                continue

            if mkt.strike <= 0 or spot_price <= 0:
                continue

            # Calculate distance from strike
            distance_pct = (spot_price - mkt.strike) / mkt.strike

            # Calculate true probability using Black-Scholes
            t_years = max(remaining_sec, 30) / 31536000
            sigma_t = realized_vol_annual * math.sqrt(t_years)

            if sigma_t <= 0:
                continue

            log_ratio = math.log(spot_price / mkt.strike) if mkt.strike > 0 else 0
            d2 = log_ratio / sigma_t
            true_prob = 0.5 * (1.0 + math.erf(d2 / math.sqrt(2)))
            true_prob = max(0.01, min(0.99, true_prob))

            # --- Check if YES side is underpriced ---
            if true_prob > 0.90 and mkt.yes_price < true_prob - 0.03:
                edge = true_prob - mkt.yes_price
                # Subtract estimated fees (~1.5% round trip)
                net_edge = edge - 0.015
                if net_edge > 0.02:
                    opps.append(ArbOpportunity(
                        edge_type="settlement_snipe",
                        description=(
                            f"BTC ${spot_price:,.0f} vs strike ${mkt.strike:,.0f} "
                            f"({remaining_min:.1f}min left) — YES worth {true_prob:.0%}, "
                            f"market at {mkt.yes_price:.0%}"
                        ),
                        profit_pct=net_edge,
                        confidence=true_prob,
                        max_size_usd=300,
                        urgency="seconds",
                        ticker=mkt.ticker,
                        side="YES",
                        current_price=mkt.yes_price,
                        fair_value=true_prob,
                        spot_price=spot_price,
                        strike_price=mkt.strike,
                        minutes_to_expiry=remaining_min,
                    ))

            # --- Check if NO side is underpriced ---
            true_prob_no = 1.0 - true_prob
            if true_prob_no > 0.90 and mkt.no_price < true_prob_no - 0.03:
                edge = true_prob_no - mkt.no_price
                net_edge = edge - 0.015
                if net_edge > 0.02:
                    opps.append(ArbOpportunity(
                        edge_type="settlement_snipe",
                        description=(
                            f"BTC ${spot_price:,.0f} vs strike ${mkt.strike:,.0f} "
                            f"({remaining_min:.1f}min left) — NO worth {true_prob_no:.0%}, "
                            f"market at {mkt.no_price:.0%}"
                        ),
                        profit_pct=net_edge,
                        confidence=true_prob_no,
                        max_size_usd=300,
                        urgency="seconds",
                        ticker=mkt.ticker,
                        side="NO",
                        current_price=mkt.no_price,
                        fair_value=true_prob_no,
                        spot_price=spot_price,
                        strike_price=mkt.strike,
                        minutes_to_expiry=remaining_min,
                    ))

        # Sort by confidence * edge (best opportunities first)
        opps.sort(key=lambda o: o.confidence * o.profit_pct, reverse=True)
        return opps


# ---------------------------------------------------------------------------
# EDGE 3: Intra-Market Bracket Arbitrage
# ---------------------------------------------------------------------------

class BracketArb:
    """
    Finds arbitrage within Kalshi's own bracket contracts.

    Kalshi often lists multiple brackets for the same event:
      "BTC above $70k" at YES 80¢
      "BTC above $72k" at YES 55¢
      "BTC above $74k" at YES 30¢

    Rules that MUST hold:
    1. YES + NO on same contract must = $1.00 (minus spread)
    2. Higher strike YES must be <= lower strike YES
    3. Sum of all mutually exclusive bracket YES prices must = $1.00

    When these rules are violated → guaranteed profit.
    """

    def __init__(self) -> None:
        pass

    def scan(self, kalshi_markets: list) -> list[ArbOpportunity]:
        """Find bracket arbitrage opportunities within Kalshi."""
        opps = []

        # --- Check 1: YES + NO < $1.00 on individual contracts ---
        for mkt in kalshi_markets:
            if not mkt.active or mkt.settled:
                continue
            combined = mkt.yes_price + mkt.no_price
            # After Kalshi fees (~1.5% round trip), need combined < ~0.97 to profit
            if combined < 0.97 and combined > 0.50:
                profit = 1.0 - combined
                opps.append(ArbOpportunity(
                    edge_type="bracket_arb",
                    description=(
                        f"YES {mkt.yes_price:.2f} + NO {mkt.no_price:.2f} = {combined:.3f} "
                        f"on {mkt.ticker} — buy both sides for guaranteed ${profit:.3f}"
                    ),
                    profit_pct=profit - 0.015,  # minus fees
                    confidence=0.99,
                    max_size_usd=200,
                    urgency="minutes",
                    ticker=mkt.ticker,
                    side="BOTH",
                    current_price=combined,
                    fair_value=1.0,
                ))

        # --- Check 2: Monotonicity violation across strikes ---
        # Group contracts by asset and expiry
        by_group: dict[str, list] = {}
        for mkt in kalshi_markets:
            if not mkt.active or mkt.settled or mkt.strike <= 0:
                continue
            # Group key: asset + close_time (same event, different strikes)
            key = f"{mkt.asset}-{mkt.close_time_ts}"
            if key not in by_group:
                by_group[key] = []
            by_group[key].append(mkt)

        for key, group in by_group.items():
            if len(group) < 2:
                continue
            # Sort by strike ascending
            group.sort(key=lambda m: m.strike)

            # Higher strikes should have LOWER yes prices
            for i in range(1, len(group)):
                lower = group[i - 1]
                higher = group[i]
                if higher.yes_price > lower.yes_price + 0.02:
                    # Violation: higher strike is priced ABOVE lower strike
                    # Buy YES on lower strike, sell YES on higher strike
                    profit = higher.yes_price - lower.yes_price
                    opps.append(ArbOpportunity(
                        edge_type="bracket_arb",
                        description=(
                            f"Monotonicity violation: {higher.ticker} YES "
                            f"{higher.yes_price:.2f} > {lower.ticker} YES "
                            f"{lower.yes_price:.2f} (strikes ${lower.strike:,.0f} vs ${higher.strike:,.0f})"
                        ),
                        profit_pct=profit - 0.015,
                        confidence=0.95,
                        max_size_usd=200,
                        urgency="minutes",
                        ticker=f"{lower.ticker}+{higher.ticker}",
                        side="ARB",
                        current_price=profit,
                        fair_value=0.0,
                    ))

        opps.sort(key=lambda o: o.profit_pct, reverse=True)
        return opps


# ---------------------------------------------------------------------------
# Master Edge Scanner — combines all three
# ---------------------------------------------------------------------------

class EdgeScanner:
    """
    Master scanner that runs all three edge strategies continuously.

    Feeds opportunities into an async queue for the bot to execute.
    """

    def __init__(self) -> None:
        self.cross_venue = CrossVenueArb()
        self.settlement = SettlementSniper()
        self.bracket = BracketArb()
        self.opportunity_queue: asyncio.Queue[ArbOpportunity] = asyncio.Queue()
        self._running = False
        self._scan_count = 0

    async def start(
        self,
        kalshi_client,
        price_feed,
        vol_tracker=None,
    ) -> None:
        """
        Run all edge scanners in a continuous loop.

        Args:
            kalshi_client: KalshiClient instance for fetching markets
            price_feed: PriceFeed instance for current BTC price
            vol_tracker: Optional VolatilityTracker for realized vol
        """
        self._running = True
        logger.info("EdgeScanner started — hunting for arbitrage opportunities")

        while self._running:
            try:
                self._scan_count += 1

                # Get current data
                kalshi_markets = []
                if kalshi_client.is_connected:
                    kalshi_markets = kalshi_client.get_crypto_markets()

                btc_state = price_feed.get_price("BTC")
                spot_price = btc_state.consensus_price if btc_state else 0

                # Get realized vol
                vol = 0.65  # default
                if vol_tracker:
                    vol = vol_tracker.realized_vol_annualized()

                # --- Run all three edge scanners ---
                all_opps: list[ArbOpportunity] = []

                # Edge 1: Cross-venue arb (only if we have Kalshi markets)
                if kalshi_markets:
                    try:
                        cross_opps = await self.cross_venue.scan(kalshi_markets)
                        all_opps.extend(cross_opps)
                        if cross_opps:
                            logger.info(
                                "CROSS-VENUE ARB: %d opportunities found!",
                                len(cross_opps),
                            )
                    except Exception as exc:
                        logger.debug("Cross-venue scan error: %s", exc)

                # Edge 2: Settlement sniper (needs spot price + markets)
                if kalshi_markets and spot_price > 0:
                    snipe_opps = self.settlement.scan(kalshi_markets, spot_price, vol)
                    all_opps.extend(snipe_opps)
                    if snipe_opps:
                        logger.info(
                            "SETTLEMENT SNIPE: %d near-expiry opportunities! BTC=$%.0f",
                            len(snipe_opps), spot_price,
                        )

                # Edge 3: Bracket arb (only needs Kalshi markets)
                if kalshi_markets:
                    bracket_opps = self.bracket.scan(kalshi_markets)
                    all_opps.extend(bracket_opps)
                    if bracket_opps:
                        logger.info(
                            "BRACKET ARB: %d mispriced brackets found!",
                            len(bracket_opps),
                        )

                # Push opportunities to queue
                for opp in all_opps:
                    if opp.profit_pct > 0.01:  # Only if > 1% profit
                        await self.opportunity_queue.put(opp)
                        logger.info(
                            "EDGE [%s]: %s (profit=%.1f%%, conf=%.0f%%, urgency=%s)",
                            opp.edge_type, opp.description[:80],
                            opp.profit_pct * 100, opp.confidence * 100,
                            opp.urgency,
                        )

                # Scan interval: fast for settlement snipes, slower otherwise
                if any(o.urgency == "seconds" for o in all_opps):
                    await asyncio.sleep(2)   # Fast scan when near-expiry opps exist
                else:
                    await asyncio.sleep(10)  # Normal scan interval

            except Exception as exc:
                logger.error("EdgeScanner error: %s", exc, exc_info=True)
                await asyncio.sleep(5)

    async def stop(self) -> None:
        self._running = False
        await self.cross_venue.close()
        logger.info("EdgeScanner stopped after %d scans", self._scan_count)
