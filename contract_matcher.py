"""
Kalshi contract matcher.

When Trump posts and the sentiment analyzer extracts keywords,
this module searches Kalshi's open markets for matching contracts
and places trades on them.

Example flow:
  Trump posts: "TARIFFS ON CHINA GOING TO 60% IMMEDIATELY!"
  Sentiment → keywords: ["tariff", "china", "trade"], side: YES, conf: 0.90
  Matcher → finds: "Will US impose >50% tariffs on China?" at YES 45c
  Trade → buys YES at 45c → contract jumps to 85c within minutes
  Profit: +89% on the position
"""

import logging
import time
from dataclasses import dataclass
from typing import Optional

import config
from kalshi_client import KalshiClient, KalshiMarket, KalshiOrder
from sentiment_analyzer import SentimentResult

logger = logging.getLogger(__name__)


@dataclass
class ContractMatch:
    """A Kalshi contract matched to a Trump post."""
    ticker: str
    title: str
    yes_price: float
    no_price: float
    volume: float
    match_score: float     # How well the keywords match (0-1)
    side: str              # "YES" or "NO" — which to buy
    confidence: float      # Combined sentiment + match confidence
    keywords_matched: list[str]


class ContractMatcher:
    """Matches Trump post sentiment to tradeable Kalshi contracts."""

    def __init__(self, kalshi: KalshiClient) -> None:
        self._kalshi = kalshi
        self._market_cache: list[KalshiMarket] = []
        self._cache_time: float = 0
        self._cache_ttl: float = 60  # Refresh market list every 60s
        self._paper_mode = config.PAPER_MODE

    def find_matches(self, sentiment: SentimentResult) -> list[ContractMatch]:
        """Find Kalshi contracts that match the sentiment keywords."""
        if not sentiment.kalshi_keywords:
            return []
        if sentiment.kalshi_confidence < 0.40:
            return []

        # Get current markets
        markets = self._get_markets()
        if not markets:
            # Paper mode: generate synthetic matches
            if self._paper_mode:
                return self._paper_matches(sentiment)
            return []

        matches: list[ContractMatch] = []
        keywords = [kw.lower() for kw in sentiment.kalshi_keywords]

        for market in markets:
            if market.settled or not market.active:
                continue

            title_lower = market.title.lower()
            ticker_lower = market.ticker.lower()

            # Count keyword matches
            matched_kw = [kw for kw in keywords if kw in title_lower or kw in ticker_lower]
            if not matched_kw:
                continue

            # Match score: what % of keywords hit
            match_score = len(matched_kw) / len(keywords)
            if match_score < 0.3:
                continue  # Need at least 30% of keywords to match

            # Determine which side to buy
            side = sentiment.kalshi_side or "YES"

            # Combined confidence
            combined_conf = sentiment.kalshi_confidence * match_score

            # Only trade if current price gives us room to profit
            our_price = market.yes_price if side == "YES" else market.no_price
            if our_price > 0.85:
                continue  # Already priced in, no upside
            if our_price < 0.03:
                continue  # Too illiquid

            matches.append(ContractMatch(
                ticker=market.ticker,
                title=market.title,
                yes_price=market.yes_price,
                no_price=market.no_price,
                volume=market.volume,
                match_score=round(match_score, 3),
                side=side,
                confidence=round(combined_conf, 3),
                keywords_matched=matched_kw,
            ))

        # Sort by confidence
        matches.sort(key=lambda m: m.confidence, reverse=True)

        # Return top 3 matches
        return matches[:3]

    def execute_match(self, match: ContractMatch, size_usd: float) -> Optional[KalshiOrder]:
        """Place a limit order on a matched contract."""
        price = match.yes_price if match.side == "YES" else match.no_price

        logger.info(
            "TRUMP CONTRACT TRADE: %s %s @ %.2f (conf=%.2f, match=%.0f%%, keywords=%s)",
            match.side, match.ticker, price, match.confidence,
            match.match_score * 100, match.keywords_matched,
        )

        return self._kalshi.place_order(
            ticker=match.ticker,
            side=match.side,
            size_usd=size_usd,
            price=price,
        )

    def _get_markets(self) -> list[KalshiMarket]:
        """Get ALL current Kalshi markets, with caching."""
        if time.time() - self._cache_time > self._cache_ttl:
            if self._kalshi.is_connected:
                all_markets: list[KalshiMarket] = []
                # Fetch crypto markets
                all_markets.extend(self._kalshi.get_crypto_markets())
                # Fetch ALL open markets (politics, economics, tariffs, etc.)
                try:
                    resp = self._kalshi._client.get_markets(status="open", limit=500)
                    seen_tickers = {m.ticker for m in all_markets}
                    for m in resp.get("markets", []):
                        parsed = self._kalshi._parse_market(m)
                        if parsed and parsed.ticker not in seen_tickers:
                            all_markets.append(parsed)
                            seen_tickers.add(parsed.ticker)
                except Exception as exc:
                    logger.debug("Failed to fetch all Kalshi markets: %s", exc)
                self._market_cache = all_markets
                self._cache_time = time.time()
                logger.info("Contract matcher cached %d Kalshi markets", len(self._market_cache))
        return self._market_cache

    def _paper_matches(self, sentiment: SentimentResult) -> list[ContractMatch]:
        """Generate synthetic matches for paper mode testing."""
        import random

        # Map keywords to realistic contract titles
        contract_templates = {
            "tariff": [
                ("TARIFF-CHINA-50PCT", "Will US impose >50% tariffs on China by end of month?", 0.45),
                ("TARIFF-EU-25PCT", "Will US impose >25% tariffs on EU goods?", 0.35),
                ("TRADE-WAR-ESCALATE", "Will a new trade war escalate this quarter?", 0.40),
            ],
            "china": [
                ("TARIFF-CHINA-50PCT", "Will US impose >50% tariffs on China by end of month?", 0.45),
                ("CHINA-RETALIATION", "Will China retaliate with counter-tariffs?", 0.55),
            ],
            "bitcoin": [
                ("BTC-RESERVE", "Will US establish a strategic Bitcoin reserve?", 0.25),
                ("CRYPTO-REG-FAVORABLE", "Will new crypto regulation be favorable?", 0.50),
            ],
            "crypto": [
                ("CRYPTO-EO", "Will Trump sign a crypto executive order this month?", 0.40),
                ("BTC-RESERVE", "Will US establish a strategic Bitcoin reserve?", 0.25),
            ],
            "fed": [
                ("FED-RATE-CUT", "Will the Fed cut rates at next meeting?", 0.38),
                ("FED-CHAIR-FIRE", "Will Trump attempt to replace the Fed chair?", 0.15),
            ],
            "executive order": [
                ("TRUMP-EO-MONTH", "Will Trump sign >5 executive orders this month?", 0.60),
            ],
            "fire": [
                ("CABINET-CHANGE", "Will there be a cabinet change this month?", 0.30),
            ],
        }

        matches: list[ContractMatch] = []
        seen = set()

        for kw in sentiment.kalshi_keywords:
            kw_lower = kw.lower()
            templates = contract_templates.get(kw_lower, [])
            for ticker, title, base_price in templates:
                if ticker in seen:
                    continue
                seen.add(ticker)

                # Add noise to price
                price = max(0.05, min(0.80, base_price + random.gauss(0, 0.05)))
                side = sentiment.kalshi_side or "YES"

                matches.append(ContractMatch(
                    ticker=ticker,
                    title=title,
                    yes_price=round(price, 4),
                    no_price=round(1.0 - price, 4),
                    volume=random.uniform(3000, 50000),
                    match_score=0.7 + random.uniform(0, 0.3),
                    side=side,
                    confidence=round(sentiment.kalshi_confidence * 0.8, 3),
                    keywords_matched=[kw],
                ))

        matches.sort(key=lambda m: m.confidence, reverse=True)
        return matches[:3]
