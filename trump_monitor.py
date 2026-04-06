"""
Trump Truth Social monitor.

Polls multiple sources for new Trump posts every 2-3 seconds:
  1. Truth Social RSS/web scrape (primary)
  2. Twitter mirror accounts (backup — @TruthSocialBot etc.)
  3. Nitter instances (backup)

When a new post is detected, emits it for sentiment analysis.
Speed target: detect a new post within 5 seconds of publication.
"""

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

import config

logger = logging.getLogger(__name__)


@dataclass
class TrumpPost:
    """A detected Trump social media post."""
    post_id: str
    text: str
    timestamp: float
    source: str           # "truthsocial", "twitter_mirror", "rss"
    detected_at: float = field(default_factory=time.time)
    url: str = ""
    has_media: bool = False
    reply_to: str = ""    # If it's a reply/repost

    @property
    def detection_latency_ms(self) -> float:
        return (self.detected_at - self.timestamp) * 1000

    @property
    def text_hash(self) -> str:
        return hashlib.md5(self.text.encode()).hexdigest()[:12]


class TrumpMonitor:
    """Monitors Trump's Truth Social for new posts with minimal latency."""

    # Truth Social user ID for Trump
    TRUMP_TS_USER_ID = "107780257626128497"
    TRUMP_TS_USERNAME = "realDonaldTrump"

    # Multiple sources for redundancy and speed
    TRUTH_SOCIAL_SOURCES = [
        # Truth Social public API / RSS endpoints
        "https://truthsocial.com/api/v1/accounts/{user_id}/statuses",
        "https://truthsocial.com/@{username}/rss",
    ]

    # Twitter mirror accounts that repost Trump's Truth Social
    TWITTER_MIRRORS = [
        # These accounts auto-repost Trump's Truth Social posts
        # Update these as accounts come and go
    ]

    def __init__(self) -> None:
        self._post_queue: asyncio.Queue[TrumpPost] = asyncio.Queue()
        self._running = False
        self._seen_hashes: set[str] = set()
        self._http = httpx.AsyncClient(
            timeout=5.0,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; NewsBot/1.0)"},
        )
        self._poll_interval = float(config.TRUMP_POLL_INTERVAL_SECONDS)
        self._last_post_time: float = time.time()

    @property
    def post_queue(self) -> asyncio.Queue[TrumpPost]:
        return self._post_queue

    async def start(self) -> None:
        self._running = True
        logger.info(
            "TrumpMonitor starting (poll_interval=%.1fs)",
            self._poll_interval,
        )

        if config.PAPER_MODE:
            await self._run_paper_mode()
        else:
            # Run all sources concurrently for fastest detection
            tasks = [
                asyncio.create_task(self._poll_truth_social(), name="truthsocial"),
                asyncio.create_task(self._poll_rss(), name="rss"),
            ]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)

    async def stop(self) -> None:
        self._running = False
        await self._http.aclose()
        logger.info("TrumpMonitor stopped")

    # --- Truth Social API Polling ---

    async def _poll_truth_social(self) -> None:
        """Poll Truth Social's public API for new statuses."""
        url = f"https://truthsocial.com/api/v1/accounts/{self.TRUMP_TS_USER_ID}/statuses"

        while self._running:
            try:
                resp = await self._http.get(
                    url,
                    params={"limit": 5, "exclude_replies": "false"},
                )
                if resp.status_code == 200:
                    posts = resp.json()
                    for post_data in posts:
                        post = self._parse_truth_social_post(post_data)
                        if post and post.text_hash not in self._seen_hashes:
                            self._seen_hashes.add(post.text_hash)
                            await self._post_queue.put(post)
                            logger.info(
                                "NEW POST detected (TruthSocial): %s... [latency=%dms]",
                                post.text[:80], post.detection_latency_ms,
                            )
                elif resp.status_code == 429:
                    logger.warning("Truth Social rate limited, backing off 10s")
                    await asyncio.sleep(10)
                else:
                    logger.debug("Truth Social returned %d", resp.status_code)

            except Exception as exc:
                logger.error("Truth Social poll error: %s", exc)

            await asyncio.sleep(self._poll_interval)

    def _parse_truth_social_post(self, data: dict) -> Optional[TrumpPost]:
        """Parse a Truth Social API response into a TrumpPost."""
        try:
            # Extract text content (strip HTML tags)
            content = data.get("content", "")
            text = re.sub(r"<[^>]+>", "", content).strip()
            if not text:
                return None

            # Parse timestamp
            created_at = data.get("created_at", "")
            try:
                from datetime import datetime, timezone
                dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                ts = dt.timestamp()
            except (ValueError, TypeError):
                ts = time.time()

            return TrumpPost(
                post_id=str(data.get("id", "")),
                text=text,
                timestamp=ts,
                source="truthsocial",
                url=data.get("url", ""),
                has_media=bool(data.get("media_attachments")),
                reply_to=str(data.get("in_reply_to_id", "") or ""),
            )
        except Exception as exc:
            logger.error("Failed to parse Truth Social post: %s", exc)
            return None

    # --- RSS Feed Polling ---

    async def _poll_rss(self) -> None:
        """Poll RSS feeds that aggregate Trump's posts."""
        rss_url = f"https://truthsocial.com/@{self.TRUMP_TS_USERNAME}/rss"

        while self._running:
            try:
                resp = await self._http.get(rss_url)
                if resp.status_code == 200:
                    posts = self._parse_rss(resp.text)
                    for post in posts:
                        if post.text_hash not in self._seen_hashes:
                            self._seen_hashes.add(post.text_hash)
                            await self._post_queue.put(post)
                            logger.info(
                                "NEW POST detected (RSS): %s... [latency=%dms]",
                                post.text[:80], post.detection_latency_ms,
                            )
            except Exception as exc:
                logger.debug("RSS poll error: %s", exc)

            # RSS is slower, poll less frequently
            await asyncio.sleep(self._poll_interval * 3)

    def _parse_rss(self, xml_text: str) -> list[TrumpPost]:
        """Parse RSS XML for Trump posts (simple regex parser for speed)."""
        posts = []
        items = re.findall(r"<item>(.*?)</item>", xml_text, re.DOTALL)
        for item in items[:5]:
            title = re.search(r"<title>(.*?)</title>", item)
            desc = re.search(r"<description>(.*?)</description>", item, re.DOTALL)
            pub_date = re.search(r"<pubDate>(.*?)</pubDate>", item)
            link = re.search(r"<link>(.*?)</link>", item)

            text = ""
            if desc:
                text = re.sub(r"<[^>]+>", "", desc.group(1)).strip()
            elif title:
                text = title.group(1).strip()

            if not text:
                continue

            ts = time.time()
            if pub_date:
                try:
                    from email.utils import parsedate_to_datetime
                    dt = parsedate_to_datetime(pub_date.group(1))
                    ts = dt.timestamp()
                except Exception:
                    pass

            posts.append(TrumpPost(
                post_id=hashlib.md5(text.encode()).hexdigest()[:16],
                text=text,
                timestamp=ts,
                source="rss",
                url=link.group(1) if link else "",
            ))
        return posts

    # --- Paper Mode ---

    async def _run_paper_mode(self) -> None:
        """Simulate Trump posts for testing."""
        logger.info("[PAPER] TrumpMonitor running in simulation mode")

        import random

        sample_posts = [
            # BTC bullish
            "Bitcoin is the future of finance! The United States should be the crypto capital of the world! 🇺🇸",
            "I am hereby ordering the establishment of a Strategic Bitcoin Reserve. America will be the crypto superpower!",
            "Crypto is BOOMING under my administration. We will NEVER let the Democrats destroy Bitcoin!",
            "Just met with the top Bitcoin miners. INCREDIBLE technology. Made in America!",
            "The Federal Reserve should CUT RATES NOW. Bitcoin and crypto will soar!",
            # BTC bearish
            "China is MANIPULATING their currency again. Tariffs going to 60% on ALL Chinese goods IMMEDIATELY!",
            "The Fed is destroying our economy. Interest rates are TOO HIGH. This will end badly for everyone!",
            "Trade war with the EU is ON. 25% tariffs on European cars starting MONDAY!",
            "Our country is being ripped off by every nation on earth. MASSIVE tariffs coming this week!",
            # Neutral / non-market
            "HAPPY EASTER to all, including the Radical Left Lunatics!",
            "Just landed in Mar-a-Lago. Beautiful day in Florida! 🌴",
            "Ratings for my speech last night were through the roof. RECORD NUMBERS!",
            "The Fake News Media is at it again. They never learn!",
        ]

        while self._running:
            # Simulate a post every 30-120 seconds in paper mode
            await asyncio.sleep(random.uniform(30, 90))
            if not self._running:
                break

            text = random.choice(sample_posts)
            post = TrumpPost(
                post_id=f"paper-{int(time.time())}",
                text=text,
                timestamp=time.time() - random.uniform(1, 5),
                source="paper",
            )

            logger.info("[PAPER] Simulated post: %s...", text[:60])
            await self._post_queue.put(post)
