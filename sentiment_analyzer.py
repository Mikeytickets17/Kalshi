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
ANALYSIS_PROMPT = """You are a trading analyst monitoring Trump's social media for EVERY market-moving signal. Analyze this post and respond ONLY with a JSON object.

Post: "{text}"

Respond with exactly this JSON format:
{{"relevant": true/false, "direction": "bullish"/"bearish"/"neutral", "confidence": 0.0-1.0, "move_pct": 0.0-0.10, "reasoning": "one sentence", "topics": ["list", "of", "topics"], "kalshi_keywords": ["keyword1", "keyword2"], "kalshi_side": "YES"/"NO"/"", "kalshi_confidence": 0.0-1.0}}

WHAT IS RELEVANT (mark relevant=true):
- ANY foreign policy: Iran, Russia, China, NATO, Israel, Ukraine, North Korea, ceasefire, war, peace deal, sanctions, military action, troop deployment
- ANY economic policy: tariffs, trade deals, spending bills, debt ceiling, government shutdown, executive orders, regulation
- ANY financial topic: Fed, rates, inflation, Bitcoin, crypto, stocks, oil, dollar, treasury
- ANY personnel changes: firing cabinet members, Fed chair, ambassadors, military leaders
- ANY legislation: bills signed, vetoed, proposed
- Threats, ultimatums, or deadlines against any country
- Market-moving announcements of ANY kind

WHAT IS NOT RELEVANT (mark relevant=false):
- Personal attacks on media/opponents with no policy content
- Rally schedules, crowd sizes, ratings
- Birthday wishes, holidays, sports commentary
- Pure campaign rhetoric with no actionable policy

DIRECTION RULES:
- Peace/ceasefire/de-escalation → BULLISH (risk-on)
- War/military strike/escalation → BEARISH (risk-off)
- Tariffs/trade war → BEARISH
- Rate cuts/pro-crypto/deregulation → BULLISH
- Sanctions/embargo → depends on target, usually BEARISH
- Firing Fed chair / institutional disruption → BEARISH

KALSHI KEYWORDS — extract terms to find prediction market contracts:
- "ceasefire with Iran" → ["iran", "ceasefire", "peace", "deal"]
- "tariffs on China 60%" → ["tariff", "china", "trade"]
- "firing the Fed chair" → ["fed", "chair", "fire", "replace"]
- "executive order on crypto" → ["executive order", "crypto", "bitcoin"]
- "NATO Article 5" → ["nato", "article 5", "military"]
- "Ukraine peace deal" → ["ukraine", "peace", "ceasefire", "russia"]
- "government shutdown" → ["shutdown", "government", "spending"]
- ANY policy topic → extract the key nouns and action

KALSHI SIDE:
- Trump ANNOUNCES he will do X → buy YES on "Will Trump do X?"
- Trump says he WON'T do X → buy NO
- Trump threatens X → buy YES (he usually follows through)
- Peace deal / ceasefire → YES on peace contracts, NO on war contracts

Be aggressive on clear signals (conf 0.8+), conservative on vague posts (conf 0.3-0.5)."""


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

        # Tariff keywords — bearish for risk assets (unless it's a deal)
        tariff_bearish = ["tariff", "tariffs", "trade war", "import tax", "duties", "trade deficit"]
        tariff_bullish = ["trade deal", "trade agreement", "trade pact", "deal with china"]
        tariff_bear_hits = sum(1 for kw in tariff_bearish if kw in text)
        tariff_bull_hits = sum(1 for kw in tariff_bullish if kw in text)
        tariff_hits = tariff_bear_hits + tariff_bull_hits
        if tariff_hits > 0:
            topics.append("tariffs")
            if tariff_bull_hits > 0:
                # Trade DEAL = bullish
                direction = "BULLISH"
                confidence = min(0.5 + tariff_bull_hits * 0.15, 0.85)
                move = min(0.02 + tariff_bull_hits * 0.01, 0.05)
                relevant = True
                reasoning = f"Trade deal/agreement ({tariff_bull_hits} signals)"
            elif not relevant or tariff_bear_hits > crypto_hits:
                direction = "BEARISH"
                confidence = min(0.4 + tariff_bear_hits * 0.15, 0.80)
                move = min(0.015 + tariff_bear_hits * 0.01, 0.05)
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

        # Geopolitical / War / Peace — these are HUGE market movers
        geo_bullish = ["ceasefire", "peace deal", "peace agreement", "peace talks",
                       "de-escalation", "troop withdrawal", "diplomatic solution",
                       "treaty signed", "hostages released", "war is over", "end the war"]
        geo_bearish = ["military strike", "missile launch", "troops deployed", "invasion",
                       "war declaration", "nuclear", "bomb", "attack on", "retaliatory strike",
                       "article 5", "martial law", "state of emergency", "blockade"]
        geo_countries = ["iran", "russia", "china", "north korea", "ukraine", "israel",
                        "gaza", "taiwan", "syria", "iraq", "nato", "eu", "european union",
                        "saudi", "venezuela", "cuba", "mexico border"]

        geo_bull_hits = sum(1 for kw in geo_bullish if kw in text)
        geo_bear_hits = sum(1 for kw in geo_bearish if kw in text)
        geo_country_hits = [c for c in geo_countries if c in text]

        # Only trigger on countries if there's also an action keyword, or if
        # bullish/bearish keywords are present. Bare country mention = skip.
        has_geo_action = geo_bull_hits > 0 or geo_bear_hits > 0
        if has_geo_action or (len(geo_country_hits) > 0 and not relevant):
            topics.append("geopolitical")
            if geo_bull_hits > geo_bear_hits:
                direction = "BULLISH"
                confidence = min(0.55 + geo_bull_hits * 0.15, 0.90)
                move = min(0.02 + geo_bull_hits * 0.015, 0.06)
                reasoning = f"Peace/de-escalation ({geo_bull_hits} signals, countries: {geo_country_hits})"
            elif geo_bear_hits > 0:
                direction = "BEARISH"
                confidence = min(0.60 + geo_bear_hits * 0.15, 0.92)
                move = min(0.025 + geo_bear_hits * 0.02, 0.08)
                reasoning = f"Military/escalation ({geo_bear_hits} signals, countries: {geo_country_hits})"
            else:
                # Country mentioned but no clear direction — still relevant
                direction = "BEARISH"  # default: geopolitical uncertainty = risk-off
                confidence = 0.40
                move = 0.01
                reasoning = f"Geopolitical mention ({geo_country_hits})"
            relevant = True
            # Generate Kalshi keywords from the countries/topics mentioned
            kalshi_kw = geo_country_hits + [kw for kw in geo_bullish + geo_bearish if kw in text]
            kalshi_side = "YES" if geo_bull_hits > geo_bear_hits else "NO"
            kalshi_conf = confidence * 0.85

        # Sanctions / Trade restrictions
        sanctions_kw = ["sanction", "embargo", "ban imports", "ban exports", "blacklist",
                       "asset freeze", "travel ban", "trade restriction", "export control"]
        sanction_hits = sum(1 for kw in sanctions_kw if kw in text)
        if sanction_hits > 0:
            if "geopolitical" not in topics:
                topics.append("sanctions")
            direction = "BEARISH"
            confidence = min(0.50 + sanction_hits * 0.15, 0.85)
            move = min(0.015 + sanction_hits * 0.01, 0.04)
            relevant = True
            reasoning = f"Sanctions/restrictions ({sanction_hits} signals)"
            kalshi_kw = [kw for kw in sanctions_kw if kw in text] + geo_country_hits
            kalshi_side = "YES"
            kalshi_conf = confidence * 0.8

        # Government / Policy actions
        gov_kw = ["government shutdown", "debt ceiling", "spending bill", "budget",
                  "impeach", "resign", "25th amendment", "veto", "signed into law",
                  "supreme court", "congress vote", "senate vote", "house vote"]
        gov_hits = sum(1 for kw in gov_kw if kw in text)
        if gov_hits > 0 and "geopolitical" not in topics:
            topics.append("government")
            relevant = True
            confidence = min(0.45 + gov_hits * 0.12, 0.80)
            move = min(0.01 + gov_hits * 0.008, 0.03)
            if any(w in text for w in ["shutdown", "impeach", "resign", "25th"]):
                direction = "BEARISH"
                reasoning = f"Political crisis ({gov_hits} signals)"
            else:
                direction = "BULLISH"
                reasoning = f"Government action ({gov_hits} signals)"
            kalshi_kw = [kw for kw in gov_kw if kw in text]
            kalshi_side = "YES"
            kalshi_conf = confidence * 0.75

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
