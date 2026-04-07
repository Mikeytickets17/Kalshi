"""
Universal news-to-trade analyzer.

Takes ANY breaking headline and outputs a complete trade plan:
  - What to trade (BTC, stocks, Kalshi contracts)
  - Which direction (buy/sell/long/short)
  - How much confidence
  - Which venue to execute on

Uses Claude API for complex headlines, rule-based for speed on
obvious signals (Fed rate cut = bullish, tariffs = bearish).

Output: a list of TradeAction objects, one per venue.
"""

import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

import httpx

import config
from news_feed import NewsItem

logger = logging.getLogger(__name__)


@dataclass
class TradeAction:
    """A specific trade to execute in response to news."""
    venue: str             # "binance_spot", "binance_futures", "kalshi_contract", "alpaca_stock"
    asset: str             # "BTC", "ETH", "SPY", "AAPL", or Kalshi ticker
    side: str              # "BUY", "SELL", "LONG", "SHORT"
    confidence: float      # 0.0-1.0
    size_pct: float        # % of portfolio to allocate
    leverage: float        # 1.0 for spot, 2-5x for futures/leverage
    hold_minutes: int      # How long to hold
    reasoning: str
    category: str          # matches news category
    urgency: str           # "immediate", "fast", "normal"
    # Kalshi-specific
    kalshi_keywords: list[str] = None
    kalshi_side: str = ""  # YES/NO on the contract

    def __post_init__(self):
        if self.kalshi_keywords is None:
            self.kalshi_keywords = []


ANALYSIS_PROMPT = """You are a trading analyst. Analyze this breaking headline and output trade actions. Respond ONLY with a JSON object.

Headline: "{headline}"
Source: {source}
Category: {category}

Respond with this JSON format:
{{"actions": [
  {{"venue": "binance_spot"/"binance_futures"/"kalshi_contract"/"alpaca_stock",
    "asset": "BTC"/"ETH"/"SPY"/"QQQ"/"AAPL"/etc,
    "side": "BUY"/"SELL"/"LONG"/"SHORT",
    "confidence": 0.0-1.0,
    "size_pct": 0.01-0.10,
    "leverage": 1-5,
    "hold_minutes": 5-120,
    "reasoning": "one sentence",
    "urgency": "immediate"/"fast"/"normal",
    "kalshi_keywords": ["keyword1", "keyword2"],
    "kalshi_side": "YES"/"NO"/""}}
]}}

TRADE RULES BY EVENT TYPE:

MONETARY POLICY:
- Fed rate cut → BUY BTC + BUY SPY + LONG futures (risk-on)
- Fed rate hike → SELL BTC + SELL stocks (risk-off)
- FOMC hawkish surprise → SELL everything
- FOMC dovish surprise → BUY everything

ECONOMIC DATA:
- CPI below expectations → BUY (rate cuts more likely)
- CPI above expectations → SELL (rates stay high)
- Jobs beat → mixed (strong economy but hawkish Fed)
- Jobs miss → BUY BTC (dovish Fed pivot)
- GDP negative → BUY BTC (rate cut expectations)

TARIFFS / TRADE:
- New tariffs announced → SELL BTC + SELL affected stocks + Kalshi YES on tariff
- Tariffs removed/reduced → BUY everything
- Trade deal signed → BUY stocks + BUY BTC

GEOPOLITICAL — THIS IS CRITICAL:
- Ceasefire / peace deal → BUY stocks + BUY BTC (massive risk-on)
- War / military strike → SELL stocks, BTC direction depends on severity
- Iran deal / diplomacy → BUY oil stocks, BUY BTC
- Iran strike / attack → SELL stocks + BUY oil
- Russia-Ukraine ceasefire → BUY European stocks + BUY BTC
- China-Taiwan escalation → SELL everything
- NATO activation → SELL stocks
- Sanctions imposed → SELL affected country's trading partners
- Hostage release / diplomatic win → BUY (risk-on)

CRYPTO-SPECIFIC:
- Bitcoin reserve / pro-crypto EO → BUY BTC aggressively
- Crypto regulation favorable → BUY BTC + BUY ETH
- Crypto ban / unfavorable reg → SELL BTC
- ETF approval → BUY BTC
- Exchange hack/failure → SELL BTC

POLITICAL:
- Government shutdown → SELL stocks short-term
- Debt ceiling crisis → SELL stocks + BUY BTC
- Presidential impeachment → SELL stocks
- Cabinet firing → depends on who and context

ALWAYS output Kalshi keywords for ANY event that could have a prediction market:
- "Iran ceasefire" → kalshi_keywords: ["iran", "ceasefire", "peace"]
- "Government shutdown" → kalshi_keywords: ["shutdown", "government"]
- "Fed rate cut" → kalshi_keywords: ["fed", "rate", "cut"]
- ANY Trump action → kalshi_keywords with the action topic

Output 1-4 actions per headline. Use multiple venues for big news.
Only leverage >1x for confidence >0.75.
Set urgency="immediate" for war/peace, FOMC, tariffs, CPI."""


class NewsAnalyzer:
    """Analyzes any news headline and produces trade actions."""

    def __init__(self) -> None:
        self._api_key = config.ANTHROPIC_API_KEY
        self._http = httpx.AsyncClient(timeout=10.0)

        # Use multi-provider AI system
        from ai_provider import AIProvider
        self._ai = AIProvider()
        self._enabled = self._ai.is_ai_enabled

        if self._enabled:
            logger.info("NewsAnalyzer initialized with AI: %s", self._ai.provider_name)
        else:
            logger.info("NewsAnalyzer running in rule-based mode (no AI keys)")

    async def analyze(self, news: NewsItem) -> list[TradeAction]:
        """Analyze a news item and return trade actions."""
        start = time.time()

        if self._enabled:
            actions = await self._analyze_with_claude(news)
        else:
            actions = self._analyze_with_rules(news)

        elapsed = (time.time() - start) * 1000
        logger.info(
            "Analyzed in %dms: %d actions from [%s] %s",
            elapsed, len(actions), news.source, news.headline[:60],
        )
        return actions

    async def _analyze_with_claude(self, news: NewsItem) -> list[TradeAction]:
        """Use best available AI for analysis (Claude, Groq, Gemini, Ollama)."""
        try:
            prompt = ANALYSIS_PROMPT.format(
                headline=news.headline[:300],
                source=news.source,
                category=news.category,
            )
            # Use multi-provider AI
            analysis = await self._ai.analyze(prompt)
            if analysis and "actions" in analysis:
                actions = []
                for a in analysis["actions"]:
                    actions.append(TradeAction(
                        venue=a.get("venue", "binance_spot"),
                        asset=a.get("asset", "BTC"),
                        side=a.get("side", "BUY"),
                        confidence=float(a.get("confidence", 0.5)),
                        size_pct=float(a.get("size_pct", 0.03)),
                        leverage=float(a.get("leverage", 1)),
                        hold_minutes=int(a.get("hold_minutes", 15)),
                        reasoning=f"[{self._ai.provider_name}] {a.get('reasoning', '')}",
                        category=news.category,
                        urgency=a.get("urgency", "normal"),
                        kalshi_keywords=a.get("kalshi_keywords", []),
                        kalshi_side=a.get("kalshi_side", ""),
                    ))
                return actions
            # If AI returned no actions, fall back to rules
            return self._analyze_with_rules(news)
        except Exception as exc:
            logger.error("AI news analysis failed: %s", exc)
            return self._analyze_with_rules(news)

    async def _analyze_with_claude_legacy(self, news: NewsItem) -> list[TradeAction]:
        """Legacy Claude-only method (kept for reference)."""
        try:
            prompt = ANALYSIS_PROMPT.format(
                headline=news.headline[:300],
                source=news.source,
                category=news.category,
            )
            resp = await self._http.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": self._api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 500,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            if resp.status_code != 200:
                return self._analyze_with_rules(news)

            text = resp.json()["content"][0]["text"].strip()
            if "```" in text:
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            data = json.loads(text.strip())

            actions = []
            for a in data.get("actions", []):
                actions.append(TradeAction(
                    venue=a.get("venue", "binance_spot"),
                    asset=a.get("asset", "BTC"),
                    side=a.get("side", "BUY"),
                    confidence=float(a.get("confidence", 0)),
                    size_pct=float(a.get("size_pct", 0.03)),
                    leverage=float(a.get("leverage", 1)),
                    hold_minutes=int(a.get("hold_minutes", 15)),
                    reasoning=a.get("reasoning", ""),
                    category=news.category,
                    urgency=a.get("urgency", "normal"),
                    kalshi_keywords=a.get("kalshi_keywords", []),
                    kalshi_side=a.get("kalshi_side", ""),
                ))
            return actions
        except Exception as exc:
            logger.error("Claude analysis failed: %s", exc)
            return self._analyze_with_rules(news)

    def _analyze_with_rules(self, news: NewsItem) -> list[TradeAction]:
        """Fast rule-based analysis for common patterns."""
        text = (news.headline + " " + news.body).lower()
        actions: list[TradeAction] = []
        cat = news.category

        # ═══ FED / RATE DECISIONS ═══
        if cat == "fed" or any(kw in text for kw in ["rate cut", "rate hike", "fomc", "federal reserve"]):
            if any(kw in text for kw in ["cut", "lower", "dovish", "easing"]):
                # Rate cut = BULLISH everything
                actions.append(TradeAction("binance_spot", "BTC", "BUY", 0.80, 0.06, 1, 30, "Fed rate cut bullish BTC", "fed", "immediate"))
                actions.append(TradeAction("binance_futures", "BTC", "LONG", 0.80, 0.04, 3, 20, "Leveraged BTC long on rate cut", "fed", "immediate"))
                actions.append(TradeAction("alpaca_stock", "SPY", "BUY", 0.75, 0.05, 1, 60, "Rate cut bullish stocks", "fed", "fast"))
                actions.append(TradeAction("kalshi_contract", "FED", "YES", 0.85, 0.04, 1, 30, "Buy YES on rate cut contract", "fed", "immediate", ["fed", "rate", "cut"], "YES"))
            elif any(kw in text for kw in ["hike", "raise", "hawkish", "tightening"]):
                actions.append(TradeAction("binance_spot", "BTC", "SELL", 0.75, 0.05, 1, 30, "Rate hike bearish BTC", "fed", "immediate"))
                actions.append(TradeAction("alpaca_stock", "SPY", "SELL", 0.70, 0.04, 1, 60, "Rate hike bearish stocks", "fed", "fast"))
            elif "fire" in text or "replace" in text:
                actions.append(TradeAction("binance_spot", "BTC", "SELL", 0.70, 0.05, 1, 30, "Fed chair uncertainty bearish", "fed", "immediate"))
                actions.append(TradeAction("kalshi_contract", "FED", "YES", 0.80, 0.04, 1, 60, "Buy YES on Fed chair change", "fed", "fast", ["fed", "chair", "fire", "replace"], "YES"))

        # ═══ TARIFFS / TRADE WAR ═══
        elif cat == "tariffs" or any(kw in text for kw in ["tariff", "trade war", "sanctions"]):
            actions.append(TradeAction("binance_spot", "BTC", "SELL", 0.70, 0.05, 1, 30, "Tariffs bearish risk assets", "tariffs", "immediate"))
            actions.append(TradeAction("alpaca_stock", "SPY", "SELL", 0.75, 0.05, 1, 45, "Tariffs bearish stocks", "tariffs", "immediate"))
            actions.append(TradeAction("kalshi_contract", "TARIFF", "YES", 0.85, 0.05, 1, 60, "Tariff announcement = YES", "tariffs", "immediate", ["tariff", "trade", "china", "import"], "YES"))
            if "china" in text:
                actions.append(TradeAction("alpaca_stock", "FXI", "SELL", 0.70, 0.03, 1, 60, "China tariffs bearish Chinese stocks", "tariffs", "fast"))

        # ═══ ECONOMIC DATA ═══
        elif cat == "economic_data":
            if any(kw in text for kw in ["cpi", "inflation"]):
                if any(kw in text for kw in ["below", "lower", "cool", "ease", "drop"]):
                    # Low CPI = bullish
                    actions.append(TradeAction("binance_spot", "BTC", "BUY", 0.75, 0.05, 1, 30, "Low CPI bullish BTC", "economic_data", "immediate"))
                    actions.append(TradeAction("binance_futures", "BTC", "LONG", 0.70, 0.03, 2, 20, "Leveraged BTC on low CPI", "economic_data", "immediate"))
                    actions.append(TradeAction("alpaca_stock", "QQQ", "BUY", 0.70, 0.05, 1, 60, "Low CPI bullish tech", "economic_data", "fast"))
                elif any(kw in text for kw in ["above", "higher", "hot", "surge", "spike"]):
                    actions.append(TradeAction("binance_spot", "BTC", "SELL", 0.70, 0.04, 1, 30, "Hot CPI bearish BTC", "economic_data", "immediate"))
                    actions.append(TradeAction("alpaca_stock", "SPY", "SELL", 0.70, 0.04, 1, 45, "Hot CPI bearish stocks", "economic_data", "immediate"))

            elif any(kw in text for kw in ["jobs", "payroll", "employment", "unemployment"]):
                if any(kw in text for kw in ["beat", "strong", "surge", "smash", "above"]):
                    actions.append(TradeAction("alpaca_stock", "SPY", "BUY", 0.65, 0.04, 1, 45, "Strong jobs bullish stocks", "economic_data", "fast"))
                    actions.append(TradeAction("binance_spot", "BTC", "BUY", 0.60, 0.03, 1, 30, "Strong economy mild BTC bullish", "economic_data", "fast"))
                elif any(kw in text for kw in ["miss", "weak", "decline", "below"]):
                    actions.append(TradeAction("binance_spot", "BTC", "BUY", 0.65, 0.04, 1, 30, "Weak jobs = rate cut expectations = BTC bullish", "economic_data", "fast"))
                    actions.append(TradeAction("alpaca_stock", "SPY", "SELL", 0.60, 0.03, 1, 45, "Weak jobs bearish stocks", "economic_data", "fast"))

            elif "recession" in text or "gdp" in text:
                if any(kw in text for kw in ["negative", "contraction", "decline", "recession"]):
                    actions.append(TradeAction("binance_spot", "BTC", "BUY", 0.65, 0.05, 1, 60, "Recession = rate cuts = BTC bullish", "economic_data", "fast"))
                    actions.append(TradeAction("alpaca_stock", "SPY", "SELL", 0.75, 0.05, 1, 60, "Recession bearish stocks", "economic_data", "immediate"))
                    actions.append(TradeAction("alpaca_stock", "TLT", "BUY", 0.70, 0.04, 1, 60, "Recession = bonds rally", "economic_data", "fast"))

        # ═══ CRYPTO ═══
        elif cat == "crypto":
            if any(kw in text for kw in ["reserve", "favorable", "approve", "etf", "adoption"]):
                actions.append(TradeAction("binance_spot", "BTC", "BUY", 0.80, 0.06, 1, 30, "Pro-crypto news bullish", "crypto", "immediate"))
                actions.append(TradeAction("binance_futures", "BTC", "LONG", 0.75, 0.04, 3, 20, "Leveraged BTC on pro-crypto", "crypto", "immediate"))
                actions.append(TradeAction("binance_spot", "ETH", "BUY", 0.70, 0.04, 1, 30, "Pro-crypto lifts all boats", "crypto", "fast"))
                actions.append(TradeAction("kalshi_contract", "CRYPTO", "YES", 0.80, 0.04, 1, 60, "Pro-crypto = YES on crypto contracts", "crypto", "immediate", ["crypto", "bitcoin", "btc", "regulation"], "YES"))
            elif any(kw in text for kw in ["ban", "crack down", "restrict", "unfavorable"]):
                actions.append(TradeAction("binance_spot", "BTC", "SELL", 0.80, 0.06, 1, 30, "Anti-crypto news bearish", "crypto", "immediate"))
                actions.append(TradeAction("binance_futures", "BTC", "SHORT", 0.70, 0.03, 2, 20, "Short BTC on regulation", "crypto", "immediate"))

        # ═══ GEOPOLITICAL — War, Peace, Iran, Russia, NATO ═══
        elif cat == "geopolitical" or any(kw in text for kw in [
            "iran", "russia", "ukraine", "nato", "china", "taiwan", "north korea",
            "israel", "gaza", "ceasefire", "peace deal", "military", "troops",
            "missile", "strike", "invasion", "war", "nuclear", "sanctions",
        ]):
            # Extract countries mentioned for Kalshi keyword matching
            countries = []
            for c in ["iran", "russia", "ukraine", "china", "taiwan", "north korea",
                       "israel", "gaza", "syria", "iraq", "nato", "eu"]:
                if c in text:
                    countries.append(c)

            if any(kw in text for kw in ["ceasefire", "peace deal", "peace agreement",
                                          "peace talks", "de-escalat", "troop withdrawal",
                                          "diplomatic", "treaty", "hostage release"]):
                # PEACE = massive risk-on rally
                actions.append(TradeAction("binance_spot", "BTC", "BUY", 0.80, 0.06, 1, 45,
                    f"Peace/ceasefire = risk-on rally ({', '.join(countries)})", "geopolitical", "immediate"))
                actions.append(TradeAction("binance_futures", "BTC", "LONG", 0.75, 0.04, 3, 30,
                    "Leveraged BTC long on peace news", "geopolitical", "immediate"))
                actions.append(TradeAction("alpaca_stock", "SPY", "BUY", 0.80, 0.06, 1, 60,
                    "Peace = stocks rally hard", "geopolitical", "immediate"))
                actions.append(TradeAction("kalshi_contract", "PEACE", "YES", 0.85, 0.05, 1, 60,
                    f"Peace contract YES ({', '.join(countries)})", "geopolitical", "immediate",
                    countries + ["ceasefire", "peace", "deal"], "YES"))

            elif any(kw in text for kw in ["war", "invasion", "military strike", "missile launch",
                                            "bomb", "attack", "retaliat", "escalat", "troops deploy",
                                            "article 5", "nuclear", "blockade"]):
                # WAR/ESCALATION = risk-off
                actions.append(TradeAction("alpaca_stock", "SPY", "SELL", 0.80, 0.06, 1, 45,
                    f"Military escalation bearish stocks ({', '.join(countries)})", "geopolitical", "immediate"))
                actions.append(TradeAction("binance_spot", "BTC", "SELL", 0.65, 0.04, 1, 30,
                    "Escalation = risk-off, BTC sells initially", "geopolitical", "immediate"))
                # Oil goes up on war
                actions.append(TradeAction("alpaca_stock", "USO", "BUY", 0.70, 0.04, 1, 60,
                    "War = oil spikes", "geopolitical", "fast"))
                # Defense stocks up
                actions.append(TradeAction("alpaca_stock", "LMT", "BUY", 0.65, 0.03, 1, 60,
                    "Military action = defense stocks up", "geopolitical", "fast"))
                actions.append(TradeAction("kalshi_contract", "WAR", "YES", 0.80, 0.04, 1, 60,
                    f"Military action = YES on conflict contracts", "geopolitical", "immediate",
                    countries + ["war", "military", "strike"], "YES"))

            elif any(kw in text for kw in ["sanction", "embargo", "ban import", "asset freeze"]):
                # Sanctions = bearish but targeted
                actions.append(TradeAction("binance_spot", "BTC", "SELL", 0.60, 0.03, 1, 30,
                    f"Sanctions on {', '.join(countries)} = risk-off", "geopolitical", "fast"))
                actions.append(TradeAction("alpaca_stock", "SPY", "SELL", 0.65, 0.04, 1, 45,
                    "New sanctions = trade uncertainty", "geopolitical", "fast"))
                actions.append(TradeAction("kalshi_contract", "SANCTIONS", "YES", 0.80, 0.04, 1, 60,
                    "Sanctions announced = YES", "geopolitical", "immediate",
                    countries + ["sanction", "embargo"], "YES"))

            else:
                # General geopolitical tension
                actions.append(TradeAction("alpaca_stock", "SPY", "SELL", 0.55, 0.03, 1, 45,
                    f"Geopolitical uncertainty ({', '.join(countries)})", "geopolitical", "fast"))
                if countries:
                    actions.append(TradeAction("kalshi_contract", "GEO", "YES", 0.60, 0.03, 1, 60,
                        f"Geopolitical event ({', '.join(countries)})", "geopolitical", "fast",
                        countries, "YES"))

        # ═══ EARNINGS ═══
        elif cat == "earnings":
            if any(kw in text for kw in ["beat", "surge", "jump", "record"]):
                # Try to extract the stock ticker from the headline
                stock = self._extract_stock(text)
                if stock:
                    actions.append(TradeAction("alpaca_stock", stock, "BUY", 0.65, 0.04, 1, 60, f"{stock} earnings beat", "earnings", "fast"))
            elif any(kw in text for kw in ["miss", "drop", "plunge", "disappoint"]):
                stock = self._extract_stock(text)
                if stock:
                    actions.append(TradeAction("alpaca_stock", stock, "SELL", 0.65, 0.04, 1, 60, f"{stock} earnings miss", "earnings", "fast"))

        # Filter out low-confidence actions
        actions = [a for a in actions if a.confidence >= 0.50]
        return actions

    def _extract_stock(self, text: str) -> str:
        """Try to extract a stock ticker from headline text."""
        stock_map = {
            "apple": "AAPL", "microsoft": "MSFT", "google": "GOOGL", "alphabet": "GOOGL",
            "amazon": "AMZN", "meta": "META", "facebook": "META", "tesla": "TSLA",
            "nvidia": "NVDA", "amd": "AMD", "netflix": "NFLX", "disney": "DIS",
            "boeing": "BA", "jpmorgan": "JPM", "goldman": "GS", "berkshire": "BRK.B",
            "walmart": "WMT", "costco": "COST", "target": "TGT",
        }
        for name, ticker in stock_map.items():
            if name in text:
                return ticker
        return ""

    async def close(self) -> None:
        await self._http.aclose()
