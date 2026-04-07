"""
Universal news feed aggregator.

Monitors every major real-time news source simultaneously:

GOVERNMENT / OFFICIAL:
  - White House press releases (whitehouse.gov RSS)
  - Federal Reserve statements (federalreserve.gov RSS)
  - SEC filings (EDGAR RSS)
  - Treasury Department
  - Bureau of Labor Statistics (jobs, CPI, PPI releases)
  - USDA (agriculture data)

NEWS WIRES (fastest public sources):
  - Reuters RSS
  - Associated Press RSS
  - Bloomberg headlines (where available)
  - CNBC breaking news RSS
  - MarketWatch breaking RSS
  - Wall Street Journal breaking RSS

SOCIAL MEDIA:
  - Trump Truth Social (existing trump_monitor.py)
  - Congressional leaders' feeds
  - Fed officials' public statements

ECONOMIC DATA:
  - BLS data releases (CPI, jobs, PPI)
  - Fed rate decisions
  - GDP releases

Each source is polled at its optimal frequency. All detected
headlines are funneled into a single queue for analysis.
"""

import asyncio
import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

import config

logger = logging.getLogger(__name__)


@dataclass
class NewsItem:
    """A detected news item from any source."""
    headline: str
    body: str
    source: str            # "reuters", "fed", "whitehouse", "bls", etc.
    url: str
    timestamp: float
    detected_at: float = field(default_factory=time.time)
    category: str = ""     # "fed", "tariffs", "earnings", "economic_data", "geopolitical"
    priority: str = "normal"  # "critical", "high", "normal", "low"

    @property
    def text_hash(self) -> str:
        return hashlib.md5(self.headline.encode()).hexdigest()[:12]

    @property
    def latency_ms(self) -> float:
        return (self.detected_at - self.timestamp) * 1000


# RSS feed configurations: (name, url, poll_interval_seconds)
RSS_FEEDS = [
    # News wires — fastest public sources
    ("reuters_top", "https://feeds.reuters.com/reuters/topNews", 10),
    ("reuters_business", "https://feeds.reuters.com/reuters/businessNews", 10),
    ("reuters_markets", "https://feeds.reuters.com/reuters/marketsNews", 10),
    ("ap_top", "https://rsshub.app/apnews/topics/apf-topnews", 15),
    ("cnbc_top", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114", 10),
    ("cnbc_markets", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258", 10),
    ("marketwatch", "https://feeds.marketwatch.com/marketwatch/topstories", 15),
    ("wsj_markets", "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain.xml", 15),

    # Government — official sources
    ("whitehouse", "https://www.whitehouse.gov/feed/", 30),
    ("fed_press", "https://www.federalreserve.gov/feeds/press_all.xml", 20),
    ("fed_speeches", "https://www.federalreserve.gov/feeds/speeches.xml", 60),
    ("treasury", "https://home.treasury.gov/system/files/feed.xml", 60),
    ("sec_press", "https://www.sec.gov/rss/news/press.xml", 30),

    # Economic data releases
    ("bls_news", "https://www.bls.gov/feed/bls_latest.rss", 30),

    # Crypto-specific
    ("coindesk", "https://www.coindesk.com/arc/outboundfeeds/rss/", 15),
    ("cointelegraph", "https://cointelegraph.com/rss", 15),

    # Political/Trump-relevant
    ("politico", "https://rss.politico.com/politics-news.xml", 20),
    ("hill", "https://thehill.com/feed/", 20),

    # Financial data
    ("yahoo_finance", "https://finance.yahoo.com/news/rssheadlines", 15),
    ("investing_com", "https://www.investing.com/rss/news.rss", 15),

    # Google News (free, no key, covers everything)
    ("google_news_world", "https://news.google.com/rss/topics/CAAqJggKIiBDQkFTRWdvSUwyMHZNRFZxYUdjU0FtVnVHZ0pWVXlnQVAB?hl=en-US&gl=US&ceid=US:en", 10),
    ("google_news_business", "https://news.google.com/rss/topics/CAAqJggKIiBDQkFTRWdvSUwyMHZNRGRqTVhZU0FtVnVHZ0pWVXlnQVAB?hl=en-US&gl=US&ceid=US:en", 10),

    # Reddit (public RSS, no API key needed)
    ("reddit_wallstreetbets", "https://www.reddit.com/r/wallstreetbets/hot.json?limit=5", 30),
    ("reddit_crypto", "https://www.reddit.com/r/CryptoCurrency/hot.json?limit=5", 30),
]


class NewsFeed:
    """Aggregates news from all sources into a single stream."""

    def __init__(self) -> None:
        self._news_queue: asyncio.Queue[NewsItem] = asyncio.Queue()
        self._running = False
        self._seen_hashes: set[str] = set()
        self._http = httpx.AsyncClient(
            timeout=8.0,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; TradingBot/1.0)"},
        )

    @property
    def news_queue(self) -> asyncio.Queue[NewsItem]:
        return self._news_queue

    async def start(self) -> None:
        self._running = True
        logger.info("NewsFeed starting — %d sources", len(RSS_FEEDS))

        # Always start real RSS feeds regardless of mode
        tasks = []
        for name, url, interval in RSS_FEEDS:
            tasks.append(
                asyncio.create_task(
                    self._poll_rss(name, url, interval),
                    name=f"rss_{name}",
                )
            )

        # Brave Search for real-time breaking news (if key set)
        if config.BRAVE_API_KEY:
            tasks.append(asyncio.create_task(self._poll_brave_search(), name="brave_search"))
            logger.info("Brave Search enabled for real-time news")

        if config.PAPER_MODE:
            tasks.append(asyncio.create_task(self._run_paper_mode(), name="paper_news"))

        await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)

    async def stop(self) -> None:
        self._running = False
        await self._http.aclose()
        logger.info("NewsFeed stopped")

    async def _poll_brave_search(self) -> None:
        """Poll Brave Search API for real-time breaking news every 30s."""
        queries = [
            "Trump breaking news today",
            "Federal Reserve rate decision",
            "breaking geopolitical news",
            "Iran ceasefire OR strike OR nuclear",
            "Kalshi prediction market",
            "Bitcoin crypto regulation",
        ]
        q_idx = 0
        while self._running:
            try:
                query = queries[q_idx % len(queries)]
                q_idx += 1
                resp = await self._http.get(
                    "https://api.search.brave.com/res/v1/news/search",
                    headers={
                        "Accept": "application/json",
                        "Accept-Encoding": "gzip",
                        "X-Subscription-Token": config.BRAVE_API_KEY,
                    },
                    params={"q": query, "count": 5, "freshness": "pd"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    for r in data.get("results", []):
                        title = r.get("title", "")
                        if title and title not in [n.headline for n in self._seen_hashes]:
                            item = NewsItem(
                                headline=title,
                                body=r.get("description", "")[:300],
                                source="Brave:" + r.get("meta_url", {}).get("hostname", "web"),
                                url=r.get("url", ""),
                                timestamp=time.time(),
                            )
                            item.priority = self._classify_priority(item)
                            if item.priority in ("critical", "high"):
                                self._seen_hashes.add(item.text_hash)
                                await self._news_queue.put(item)
                                logger.info("[BRAVE] %s: %s", item.priority.upper(), title[:80])
                elif resp.status_code == 429:
                    logger.warning("Brave Search rate limited, backing off 60s")
                    await asyncio.sleep(60)
            except Exception as exc:
                logger.debug("Brave Search error: %s", exc)
            await asyncio.sleep(30)

    async def _poll_rss(self, name: str, url: str, interval: int) -> None:
        """Poll a single RSS feed (handles both XML and Reddit JSON)."""
        while self._running:
            try:
                resp = await self._http.get(url)
                if resp.status_code == 200:
                    # Handle Reddit JSON endpoints
                    if "reddit.com" in url and url.endswith(".json?limit=5"):
                        items = self._parse_reddit(resp.text, name)
                    else:
                        items = self._parse_rss(resp.text, name)
                    for item in items:
                        if item.text_hash not in self._seen_hashes:
                            self._seen_hashes.add(item.text_hash)
                            # Classify priority
                            item.priority = self._classify_priority(item)
                            if item.priority in ("critical", "high"):
                                await self._news_queue.put(item)
                                logger.info(
                                    "[%s] %s: %s",
                                    item.priority.upper(), name, item.headline[:80],
                                )
            except Exception as exc:
                logger.debug("RSS %s error: %s", name, exc)

            await asyncio.sleep(interval)

    def _parse_reddit(self, json_text: str, source: str) -> list[NewsItem]:
        """Parse Reddit JSON response for top posts."""
        items = []
        try:
            import json
            data = json.loads(json_text)
            for post in data.get("data", {}).get("children", [])[:5]:
                pd = post.get("data", {})
                title = pd.get("title", "")
                if not title:
                    continue
                items.append(NewsItem(
                    headline=title,
                    body=pd.get("selftext", "")[:300],
                    source=source,
                    url=f"https://reddit.com{pd.get('permalink', '')}",
                    timestamp=pd.get("created_utc", time.time()),
                ))
        except Exception as exc:
            logger.debug("Reddit parse error: %s", exc)
        return items

    def _parse_rss(self, xml: str, source: str) -> list[NewsItem]:
        """Fast regex RSS parser — no XML library needed for speed."""
        items = []
        entries = re.findall(r"<item>(.*?)</item>", xml, re.DOTALL)
        if not entries:
            entries = re.findall(r"<entry>(.*?)</entry>", xml, re.DOTALL)

        for entry in entries[:10]:
            title = self._extract_tag(entry, "title")
            desc = self._extract_tag(entry, "description") or self._extract_tag(entry, "summary")
            link = self._extract_tag(entry, "link")
            pub = self._extract_tag(entry, "pubDate") or self._extract_tag(entry, "published")

            if not title:
                continue

            # Clean HTML
            title = re.sub(r"<[^>]+>", "", title).strip()
            desc = re.sub(r"<[^>]+>", "", desc or "").strip()

            ts = time.time()
            if pub:
                try:
                    from email.utils import parsedate_to_datetime
                    ts = parsedate_to_datetime(pub).timestamp()
                except Exception:
                    try:
                        from datetime import datetime
                        dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
                        ts = dt.timestamp()
                    except Exception:
                        pass

            items.append(NewsItem(
                headline=title,
                body=desc[:500],
                source=source,
                url=link or "",
                timestamp=ts,
            ))
        return items

    def _extract_tag(self, text: str, tag: str) -> Optional[str]:
        match = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", text, re.DOTALL)
        return match.group(1).strip() if match else None

    def _classify_priority(self, item: NewsItem) -> str:
        """Classify news priority based on keywords."""
        text = (item.headline + " " + item.body).lower()

        # CRITICAL — these move markets instantly
        critical_kw = [
            # Monetary policy
            "federal reserve", "rate decision", "rate cut", "cuts rate", "rate hike",
            "fomc", "interest rate", "fed cut", "fed hike",
            # Economic data
            " cpi ", "cpi report", "cpi comes", "cpi data",
            "inflation data", "inflation rate",
            "jobs report", "nonfarm payroll", "nonfarm payrolls",
            "gdp growth", "gdp report", "recession",
            "debt ceiling", "government shutdown",
            # Trade/tariffs
            "tariff", "trade war", "trade deal",
            # Crypto
            "bitcoin", "ethereum", "bitcoin reserve", "crypto regulation",
            "strategic bitcoin", "crypto executive", "digital asset",
            # Trump
            "trump", "truth social", "executive order", "emergency declaration",
            # Geopolitical — EVERYTHING that moves markets
            "war ", "invasion", "military strike", "nuclear",
            "ceasefire", "peace deal", "peace agreement", "peace talks",
            "nato", "article 5",
            "iran", "iran strike", "iran deal", "iran nuclear",
            "russia", "ukraine", "kyiv", "moscow",
            "china", "taiwan", "beijing",
            "north korea", "pyongyang", "missile launch",
            "israel", "gaza", "hezbollah", "hamas",
            "sanctions", "embargo", "blockade",
            "troops deploy", "military action", "air strike",
            "hostage", "prisoner exchange",
            "coup", "assassination", "regime change",
            # Breaking
            "just in:", "breaking:",
        ]
        if any(kw in text for kw in critical_kw):
            item.category = self._categorize(text)
            return "critical"

        # HIGH — notable market movers
        high_kw = [
            "breaking", "just in", "alert",
            "earnings beat", "earnings miss", "revenue",
            "layoffs", "bankruptcy", "merger", "acquisition",
            "sec charges", "investigation",
            "oil price", "opec", "crude",
            "china", "russia", "iran",
            "treasury yield", "bond market",
            "stock market", "s&p 500", "nasdaq", "dow jones",
            "crypto", "altcoin",
            "trump administration", "white house",
            "congressional", "federal deficit",
            "inflation report", "housing data",
            "consumer confidence", "manufacturing",
            "unemployment",
        ]
        if any(kw in text for kw in high_kw):
            item.category = self._categorize(text)
            return "high"

        return "normal"

    def _categorize(self, text: str) -> str:
        if any(kw in text for kw in ["tariff", "trade war", "sanction", "import"]):
            return "tariffs"
        if any(kw in text for kw in ["cpi", "inflation", "jobs", "payroll", "gdp", "employment", "nonfarm", "unemployment"]):
            return "economic_data"
        if any(kw in text for kw in ["fed", "fomc", "rate", "interest", "monetary"]):
            return "fed"
        if any(kw in text for kw in ["bitcoin", "crypto", "ethereum", "btc"]):
            return "crypto"
        if any(kw in text for kw in ["earning", "revenue", "profit", "guidance"]):
            return "earnings"
        if any(kw in text for kw in ["war", "military", "invasion", "missile"]):
            return "geopolitical"
        return "general"

    # --- Paper Mode ---

    async def _run_paper_mode(self) -> None:
        """Simulate breaking news events."""
        logger.info("[PAPER] NewsFeed running in simulation mode")
        import random

        sample_news = [
            # Critical — immediate market movers
            {"h": "BREAKING: Federal Reserve cuts interest rates by 50 basis points", "cat": "fed", "pri": "critical"},
            {"h": "BREAKING: Trump announces 60% tariffs on all Chinese goods effective immediately", "cat": "tariffs", "pri": "critical"},
            {"h": "JUST IN: US CPI comes in at 2.1%, well below expectations of 2.5%", "cat": "economic_data", "pri": "critical"},
            {"h": "BREAKING: Nonfarm payrolls add 350,000 jobs, smashing estimates of 200,000", "cat": "economic_data", "pri": "critical"},
            {"h": "ALERT: Trump signs executive order establishing Strategic Bitcoin Reserve", "cat": "crypto", "pri": "critical"},
            {"h": "BREAKING: US GDP growth revised down to -0.3%, recession fears mount", "cat": "economic_data", "pri": "critical"},
            {"h": "JUST IN: SEC approves spot Ethereum ETFs, effective next week", "cat": "crypto", "pri": "critical"},
            {"h": "BREAKING: Trump fires Federal Reserve Chair, markets in turmoil", "cat": "fed", "pri": "critical"},
            {"h": "ALERT: China retaliates with 45% tariffs on US goods, trade war escalates", "cat": "tariffs", "pri": "critical"},
            {"h": "BREAKING: Government shutdown averted, spending bill passes Senate", "cat": "general", "pri": "critical"},
            # High — significant movers
            {"h": "Apple beats earnings estimates, iPhone revenue up 15%", "cat": "earnings", "pri": "high"},
            {"h": "NVIDIA stock drops 8% on AI chip export restrictions to China", "cat": "earnings", "pri": "high"},
            {"h": "Oil surges 5% after OPEC announces surprise production cut", "cat": "geopolitical", "pri": "high"},
            {"h": "Treasury yields spike to 5.2% as inflation fears persist", "cat": "economic_data", "pri": "high"},
            {"h": "Bitcoin surges past $75,000 as institutional buying accelerates", "cat": "crypto", "pri": "high"},
            {"h": "S&P 500 hits all-time high on strong earnings season", "cat": "general", "pri": "high"},
            {"h": "Tesla announces 2-for-1 stock split, shares jump 12%", "cat": "earnings", "pri": "high"},
            {"h": "Russia-Ukraine ceasefire agreement reached, markets rally", "cat": "geopolitical", "pri": "high"},
        ]

        while self._running:
            await asyncio.sleep(random.uniform(15, 45))
            if not self._running:
                break

            news = random.choice(sample_news)
            item = NewsItem(
                headline=news["h"],
                body="",
                source="paper",
                url="",
                timestamp=time.time() - random.uniform(1, 5),
                category=news["cat"],
                priority=news["pri"],
            )
            logger.info("[PAPER] NEWS: [%s] %s", item.priority.upper(), item.headline[:80])
            await self._news_queue.put(item)
