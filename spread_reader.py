"""
Polymarket-Coinbase Spread Reader.

Reads BTC/ETH prices from both Polymarket CLOB and Coinbase in real-time.
Detects when Polymarket contract prices diverge from Coinbase spot.
When the spread exceeds the threshold, signals the bot to trade.

Polymarket Gamma API is PUBLIC — no authentication needed for price data.
Coinbase WebSocket is PUBLIC — free real-time BTC/ETH prices.

The spread = what Polymarket thinks BTC will do vs what it's actually doing.
When Polymarket is slow to update, we buy the correct side before it adjusts.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

POLYMARKET_GAMMA = "https://gamma-api.polymarket.com"
COINBASE_API = "https://api.coinbase.com/v2/prices"


@dataclass
class SpreadOpportunity:
    """A detected spread between Polymarket and Coinbase."""
    asset: str              # BTC or ETH
    poly_yes_price: float   # Polymarket YES price for "above" contract
    poly_no_price: float
    poly_implied_prob: float  # What Polymarket thinks
    spot_price: float       # Coinbase real price
    strike: float           # Contract strike price
    true_prob: float        # What the real price implies
    spread: float           # true_prob - poly_implied_prob
    direction: str          # "YES" (buy YES, spot above strike) or "NO"
    contract_id: str
    question: str
    minutes_to_expiry: float
    timestamp: float = field(default_factory=time.time)


class SpreadReader:
    """Reads spreads between Polymarket and Coinbase in real-time."""

    def __init__(self) -> None:
        self._http = httpx.AsyncClient(timeout=10.0)
        self._running = False
        self._coinbase_btc: float = 0
        self._coinbase_eth: float = 0
        self._last_coinbase_update: float = 0
        self._opportunities: list[SpreadOpportunity] = []
        self.opportunity_queue: asyncio.Queue[SpreadOpportunity] = asyncio.Queue()

    async def start(self) -> None:
        """Start monitoring spreads."""
        self._running = True
        logger.info("SpreadReader started — monitoring Polymarket vs Coinbase")
        tasks = [
            asyncio.create_task(self._poll_coinbase(), name="coinbase_prices"),
            asyncio.create_task(self._poll_polymarket(), name="poly_spreads"),
        ]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)

    async def stop(self) -> None:
        self._running = False
        await self._http.aclose()

    async def _poll_coinbase(self) -> None:
        """Poll Coinbase for real-time BTC/ETH spot prices."""
        while self._running:
            try:
                # BTC
                resp = await self._http.get(f"{COINBASE_API}/BTC-USD/spot")
                if resp.status_code == 200:
                    data = resp.json()
                    self._coinbase_btc = float(data["data"]["amount"])
                    self._last_coinbase_update = time.time()

                # ETH
                resp2 = await self._http.get(f"{COINBASE_API}/ETH-USD/spot")
                if resp2.status_code == 200:
                    data2 = resp2.json()
                    self._coinbase_eth = float(data2["data"]["amount"])

            except Exception as exc:
                logger.debug("Coinbase price error: %s", exc)

            await asyncio.sleep(2)  # Poll every 2 seconds

    async def _poll_polymarket(self) -> None:
        """Poll Polymarket for BTC/ETH contract prices and compare to Coinbase."""
        # Wait for Coinbase prices first
        await asyncio.sleep(5)

        while self._running:
            try:
                if self._coinbase_btc <= 0:
                    await asyncio.sleep(2)
                    continue

                # Fetch BTC contracts from Polymarket
                contracts = await self._get_poly_btc_contracts()

                for contract in contracts:
                    opp = self._check_spread(contract)
                    if opp:
                        self._opportunities.append(opp)
                        await self.opportunity_queue.put(opp)
                        logger.info(
                            "SPREAD: %s %s spread=%.1f%% poly=%.2f true=%.2f spot=$%.0f strike=$%.0f (%s)",
                            opp.direction, opp.asset, opp.spread * 100,
                            opp.poly_implied_prob, opp.true_prob,
                            opp.spot_price, opp.strike, opp.question[:40],
                        )

            except Exception as exc:
                logger.debug("Polymarket spread error: %s", exc)

            await asyncio.sleep(5)  # Check every 5 seconds

    async def _get_poly_btc_contracts(self) -> list[dict]:
        """Fetch active BTC/ETH contracts from Polymarket Gamma API (public, no auth)."""
        contracts = []
        try:
            resp = await self._http.get(
                f"{POLYMARKET_GAMMA}/markets",
                params={"active": "true", "closed": "false", "limit": 50},
            )
            if resp.status_code != 200:
                return []

            markets = resp.json()
            for m in markets:
                question = (m.get("question", "") or "").lower()
                if not any(kw in question for kw in ["bitcoin", "btc", "ethereum", "eth"]):
                    continue

                tokens = m.get("tokens", [])
                yes_price = 0.5
                no_price = 0.5
                yes_token = ""
                no_token = ""
                for t in tokens:
                    outcome = (t.get("outcome", "") or "").lower()
                    if outcome == "yes":
                        yes_price = float(t.get("price", 0.5))
                        yes_token = t.get("token_id", "")
                    elif outcome == "no":
                        no_price = float(t.get("price", 0.5))
                        no_token = t.get("token_id", "")

                # Extract strike price from question
                strike = self._extract_strike(m.get("question", ""))
                asset = "BTC" if "btc" in question or "bitcoin" in question else "ETH"

                # Get end date for time to expiry
                end_date = m.get("end_date_iso", "") or m.get("end_date", "")
                expiry_ts = 0
                if end_date:
                    try:
                        from datetime import datetime
                        dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                        expiry_ts = dt.timestamp()
                    except (ValueError, TypeError):
                        pass

                contracts.append({
                    "condition_id": m.get("condition_id", ""),
                    "question": m.get("question", ""),
                    "asset": asset,
                    "strike": strike,
                    "yes_price": yes_price,
                    "no_price": no_price,
                    "yes_token": yes_token,
                    "no_token": no_token,
                    "volume": float(m.get("volume", 0) or 0),
                    "liquidity": float(m.get("liquidity", 0) or 0),
                    "expiry_ts": expiry_ts,
                })

        except Exception as exc:
            logger.debug("Polymarket fetch error: %s", exc)

        return contracts

    def _check_spread(self, contract: dict) -> Optional[SpreadOpportunity]:
        """Compare Polymarket contract price to Coinbase spot price."""
        import math

        asset = contract["asset"]
        strike = contract["strike"]
        yes_price = contract["yes_price"]
        no_price = contract["no_price"]
        expiry_ts = contract["expiry_ts"]

        if strike <= 0:
            return None

        # Get real spot price from Coinbase
        spot = self._coinbase_btc if asset == "BTC" else self._coinbase_eth
        if spot <= 0:
            return None

        # Calculate time to expiry
        remaining_sec = max(expiry_ts - time.time(), 60) if expiry_ts > 0 else 900
        minutes_left = remaining_sec / 60

        # Calculate true probability using Black-Scholes
        distance_pct = (spot - strike) / strike
        vol_annual = 0.65 if asset == "BTC" else 0.80
        t_years = max(remaining_sec, 30) / 31536000
        sigma_t = vol_annual * math.sqrt(t_years)

        if sigma_t <= 0:
            return None

        log_ratio = math.log(spot / strike) if strike > 0 else 0
        d2 = log_ratio / sigma_t
        true_prob = 0.5 * (1.0 + math.erf(d2 / math.sqrt(2)))
        true_prob = max(0.02, min(0.98, true_prob))

        # Check spread: YES side underpriced?
        spread_yes = true_prob - yes_price
        # Check spread: NO side underpriced?
        spread_no = (1.0 - true_prob) - no_price

        # Need at least 3% spread after ~1.5% fees to be worth trading
        min_spread = 0.03

        if spread_yes >= min_spread:
            return SpreadOpportunity(
                asset=asset,
                poly_yes_price=yes_price,
                poly_no_price=no_price,
                poly_implied_prob=yes_price,
                spot_price=spot,
                strike=strike,
                true_prob=true_prob,
                spread=spread_yes,
                direction="YES",
                contract_id=contract["condition_id"],
                question=contract["question"],
                minutes_to_expiry=minutes_left,
            )
        elif spread_no >= min_spread:
            return SpreadOpportunity(
                asset=asset,
                poly_yes_price=yes_price,
                poly_no_price=no_price,
                poly_implied_prob=1.0 - no_price,
                spot_price=spot,
                strike=strike,
                true_prob=true_prob,
                spread=spread_no,
                direction="NO",
                contract_id=contract["condition_id"],
                question=contract["question"],
                minutes_to_expiry=minutes_left,
            )

        return None

    def _extract_strike(self, question: str) -> float:
        """Extract strike price from Polymarket question."""
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

    def get_latest_spreads(self) -> list[SpreadOpportunity]:
        """Get recent spread opportunities."""
        cutoff = time.time() - 300  # Last 5 minutes
        return [o for o in self._opportunities if o.timestamp > cutoff]

    @property
    def coinbase_btc(self) -> float:
        return self._coinbase_btc

    @property
    def coinbase_eth(self) -> float:
        return self._coinbase_eth
