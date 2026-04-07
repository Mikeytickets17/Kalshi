"""
Sentiment analyzer — Claude API-powered market impact scoring.

Takes a Trump post, sends it to Claude for instant analysis:
  - Is this about crypto/BTC/economy/tariffs/Fed?
  - Bullish or bearish for BTC?
  - How confident? (0.0 to 1.0)
  - Expected magnitude of BTC move?

Speed target: analysis complete in <2 seconds.
"""

import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

import httpx

import config
from trump_monitor import TrumpPost

logger = logging.getLogger(__name__)


@dataclass
class SentimentResult:
    """Result of analyzing a Trump post for market impact."""
    post: TrumpPost
    is_market_relevant: bool
    direction: str              # "bullish", "bearish", "neutral"
    confidence: float           # 0.0 to 1.0
    expected_move_pct: float    # Expected BTC % move
    reasoning: str
    analysis_time_ms: float
    topics: list[str]
    # Kalshi contract matching — which Trump-related contracts to trade
    kalshi_keywords: list[str] = None  # Search terms for matching Kalshi contracts
    kalshi_side: str = ""              # "YES" or "NO" on the matched contract
    kalshi_confidence: float = 0.0     # Separate confidence for the contract trade

    def __post_init__(self):
        if self.kalshi_keywords is None:
            self.kalshi_keywords = []


# Pre-built prompt for maximum speed — no wasted tokens
ANALYSIS_PROMPT = """Analyze this Trump social media post for trading impact. Respond ONLY with a JSON object, no other text.

Post: "{text}"

Respond with exactly this JSON format:
{{"relevant": true/false, "direction": "bullish"/"bearish"/"neutral", "confidence": 0.0-1.0, "move_pct": 0.0-0.10, "reasoning": "one sentence", "topics": ["crypto","tariffs","fed","trade_war","economy","regulation"], "kalshi_keywords": ["keyword1", "keyword2"], "kalshi_side": "YES"/"NO"/"", "kalshi_confidence": 0.0-1.0}}

Rules:
- "relevant" = true if post affects BTC/crypto, tariffs, trade policy, Fed/rates, or USD
- Personal attacks, rally talk, media complaints = NOT relevant
- "direction" = BTC price direction (bullish/bearish/neutral)
- "move_pct" = expected BTC move in 30 minutes (0.01 = 1%)
- "kalshi_keywords" = search terms to find related Kalshi prediction contracts
  Examples: if post mentions tariffs on China → ["tariff", "china", "trade"]
  If post mentions firing someone → ["fire", person's name]
  If post mentions executive order → ["executive order", topic]
  If post mentions rate cuts → ["fed", "rate", "interest"]
- "kalshi_side" = which side to buy on the matching Kalshi contract
  If Trump ANNOUNCES he will do X → buy YES on "Will Trump do X?"
  If Trump says he WON'T do X → buy NO
- "kalshi_confidence" = how certain the post makes the contract outcome (0.8+ for announcements)
- Be conservative — only >0.7 confidence for unmistakable signals"""


class SentimentAnalyzer:
    """Analyzes Trump posts using Claude API for BTC market impact."""

    def __init__(self) -> None:
        self._api_key = config.ANTHROPIC_API_KEY
        self._http = httpx.AsyncClient(timeout=10.0)
        self._enabled = bool(self._api_key)

        if self._enabled:
            logger.info("SentimentAnalyzer initialized with Claude API")
        else:
            logger.info("SentimentAnalyzer running in rule-based mode (no API key)")

    async def analyze(self, post: TrumpPost) -> SentimentResult:
        """Analyze a post for BTC market impact."""
        start = time.time()

        if self._enabled:
            result = await self._analyze_with_claude(post)
        else:
            result = self._analyze_with_rules(post)

        result.analysis_time_ms = (time.time() - start) * 1000

        logger.info(
            "Sentiment: %s (conf=%.2f, move=%.1f%%, time=%dms) — %s",
            result.direction, result.confidence,
            result.expected_move_pct * 100, result.analysis_time_ms,
            result.reasoning[:60],
        )
        return result

    # --- Claude API Analysis ---

    async def _analyze_with_claude(self, post: TrumpPost) -> SentimentResult:
        """Send post to Claude API for analysis."""
        try:
            prompt = ANALYSIS_PROMPT.format(text=post.text[:500])

            resp = await self._http.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": self._api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 200,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )

            if resp.status_code != 200:
                logger.error("Claude API error %d: %s", resp.status_code, resp.text[:200])
                return self._analyze_with_rules(post)

            data = resp.json()
            text_response = data["content"][0]["text"].strip()

            # Parse JSON from Claude's response
            # Handle potential markdown wrapping
            json_str = text_response
            if "```" in json_str:
                json_str = json_str.split("```")[1]
                if json_str.startswith("json"):
                    json_str = json_str[4:]
            json_str = json_str.strip()

            analysis = json.loads(json_str)

            return SentimentResult(
                post=post,
                is_market_relevant=analysis.get("relevant", False),
                direction=analysis.get("direction", "neutral").upper(),
                confidence=float(analysis.get("confidence", 0.0)),
                expected_move_pct=float(analysis.get("move_pct", 0.0)),
                reasoning=analysis.get("reasoning", ""),
                analysis_time_ms=0,
                topics=analysis.get("topics", []),
                kalshi_keywords=analysis.get("kalshi_keywords", []),
                kalshi_side=analysis.get("kalshi_side", ""),
                kalshi_confidence=float(analysis.get("kalshi_confidence", 0.0)),
            )

        except json.JSONDecodeError as exc:
            logger.error("Failed to parse Claude response: %s", exc)
            return self._analyze_with_rules(post)
        except Exception as exc:
            logger.error("Claude API call failed: %s", exc)
            return self._analyze_with_rules(post)

    # --- Rule-Based Fallback ---

    def _analyze_with_rules(self, post: TrumpPost) -> SentimentResult:
        """Fast keyword-based analysis when Claude API is unavailable."""
        text = post.text.lower()
        topics = []
        direction = "NEUTRAL"
        confidence = 0.0
        move = 0.0
        relevant = False
        reasoning = "Rule-based analysis"

        # Crypto/BTC keywords — strongly bullish
        crypto_keywords = ["bitcoin", "btc", "crypto", "digital asset", "blockchain",
                          "crypto capital", "bitcoin reserve", "strategic reserve"]
        crypto_hits = sum(1 for kw in crypto_keywords if kw in text)
        if crypto_hits > 0:
            topics.append("crypto")
            direction = "BULLISH"
            confidence = min(0.5 + crypto_hits * 0.15, 0.90)
            move = min(0.02 + crypto_hits * 0.01, 0.06)
            relevant = True
            reasoning = f"Direct crypto mention ({crypto_hits} keywords)"

        # Tariff keywords — bearish for risk assets
        tariff_keywords = ["tariff", "tariffs", "trade war", "import tax", "duties",
                          "trade deal", "trade deficit"]
        tariff_hits = sum(1 for kw in tariff_keywords if kw in text)
        if tariff_hits > 0:
            topics.append("tariffs")
            if not relevant or tariff_hits > crypto_hits:
                direction = "BEARISH"
                confidence = min(0.4 + tariff_hits * 0.15, 0.80)
                move = min(0.015 + tariff_hits * 0.01, 0.05)
                relevant = True
                reasoning = f"Tariff/trade war mention ({tariff_hits} keywords)"

        # Fed / rate keywords — usually bullish (rate cuts = risk on)
        fed_keywords = ["federal reserve", "the fed", "interest rate", "rate cut",
                       "cut rates", "rates too high", "monetary policy"]
        fed_hits = sum(1 for kw in fed_keywords if kw in text)
        if fed_hits > 0:
            topics.append("fed")
            if not relevant:
                direction = "BULLISH"
                confidence = min(0.4 + fed_hits * 0.12, 0.75)
                move = min(0.01 + fed_hits * 0.008, 0.03)
                relevant = True
                reasoning = f"Fed/rates mention ({fed_hits} keywords)"

        # Economy keywords — context dependent
        econ_keywords = ["economy", "gdp", "recession", "inflation", "jobs report",
                        "unemployment", "stock market", "markets"]
        econ_hits = sum(1 for kw in econ_keywords if kw in text)
        if econ_hits > 0 and not relevant:
            topics.append("economy")
            # Positive economy talk = mild bullish, negative = mild bearish
            negative_words = ["recession", "crash", "disaster", "terrible", "worst"]
            if any(w in text for w in negative_words):
                direction = "BEARISH"
                confidence = 0.35
                move = 0.01
            else:
                direction = "BULLISH"
                confidence = 0.30
                move = 0.008
            relevant = True
            reasoning = f"Economy mention ({econ_hits} keywords)"

        # Amplifiers
        if any(w in text for w in ["immediately", "effective immediately", "right now", "today"]):
            confidence = min(confidence + 0.15, 0.95)
            move *= 1.5
            reasoning += " + urgency amplifier"

        if text.isupper() or text.count("!") >= 3:
            confidence = min(confidence + 0.05, 0.95)
            move *= 1.2
            reasoning += " + emphasis"

        # Generate Kalshi contract matching keywords
        kalshi_kw: list[str] = []
        kalshi_side = ""
        kalshi_conf = 0.0

        if "tariffs" in topics:
            kalshi_kw = ["tariff", "trade", "china", "import"]
            kalshi_side = "YES"  # Trump announcing tariffs = YES on tariff contracts
            kalshi_conf = confidence * 0.9
        if "crypto" in topics:
            kalshi_kw = ["bitcoin", "crypto", "btc", "digital"]
            kalshi_side = "YES"  # Trump pro-crypto = YES on crypto-friendly contracts
            kalshi_conf = confidence * 0.85
        if "fed" in topics:
            kalshi_kw = ["fed", "rate", "interest", "reserve"]
            kalshi_side = "YES" if "cut" in text else "NO"
            kalshi_conf = confidence * 0.7

        # Check for specific policy announcements (highest confidence)
        policy_patterns = {
            "executive order": (["executive order"], "YES", 0.85),
            "hereby order": (["executive order"], "YES", 0.90),
            "effective immediately": (kalshi_kw, kalshi_side, 0.90),
            "i am signing": (["executive order", "signing"], "YES", 0.88),
            "fired": (["fire", "remove"], "YES", 0.80),
            "terminate": (["fire", "remove", "terminate"], "YES", 0.80),
            "nomination": (["nominate", "appoint"], "YES", 0.75),
            "i am appointing": (["appoint", "nominate"], "YES", 0.85),
        }
        for pattern, (kw, side, conf) in policy_patterns.items():
            if pattern in text:
                kalshi_kw = kw + kalshi_kw
                kalshi_side = side
                kalshi_conf = max(kalshi_conf, conf)
                relevant = True
                if not topics:
                    topics.append("policy")

        return SentimentResult(
            post=post,
            is_market_relevant=relevant,
            direction=direction,
            confidence=round(confidence, 3),
            expected_move_pct=round(move, 4),
            reasoning=reasoning,
            analysis_time_ms=0,
            topics=topics,
            kalshi_keywords=kalshi_kw,
            kalshi_side=kalshi_side,
            kalshi_confidence=round(kalshi_conf, 3),
        )

    async def close(self) -> None:
        await self._http.aclose()
