"""
Trump Truth Social monitor.

Polls multiple sources for new Trump posts every 2-3 seconds:
  1. Truth Social RSS/web scrape (primary)
  2. Twitter/X API v2 timeline + search (if bearer token configured)
  3. Twitter mirror accounts (backup — @TruthSocialBot etc.)
  4. Nitter instances (backup)

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

import random as _random

import httpx

import config

logger = logging.getLogger(__name__)


@dataclass
class TrumpPost:
    """A detected Trump social media post."""
    post_id: str
    text: str
    timestamp: float
    source: str           # "truthsocial", "twitter", "twitter_mirror", "rss"
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

    # Nitter instances that mirror Truth Social content via RSS
    NITTER_INSTANCES = [
        "https://nitter.privacydev.net/realDonaldTrump/rss",
        "https://nitter.poast.org/realDonaldTrump/rss",
        "https://nitter.cz/realDonaldTrump/rss",
    ]

    # Rotating User-Agent strings to avoid rate limiting
    USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
        "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    ]

    def __init__(self) -> None:
        self._post_queue: asyncio.Queue[TrumpPost] = asyncio.Queue()
        self._running = False
        self._seen_hashes: set[str] = set()
        self._ua_index = 0
        self._http = httpx.AsyncClient(
            timeout=5.0,
            follow_redirects=True,
            headers={"User-Agent": self._next_user_agent()},
        )
        self._poll_interval = float(config.TRUMP_POLL_INTERVAL_SECONDS)
        self._last_post_time: float = time.time()

    def _next_user_agent(self) -> str:
        """Return the next User-Agent string in the rotation."""
        ua = self.USER_AGENTS[self._ua_index % len(self.USER_AGENTS)]
        self._ua_index += 1
        return ua

    def _rotate_user_agent(self) -> None:
        """Rotate the User-Agent header on the HTTP client."""
        self._http.headers["User-Agent"] = self._next_user_agent()

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
                asyncio.create_task(self._poll_nitter(), name="nitter"),
                asyncio.create_task(self._poll_truth_atom(), name="truth_atom"),
            ]
            # Add Twitter API pollers if bearer token is configured
            if config.TWITTER_BEARER_TOKEN:
                tasks.append(
                    asyncio.create_task(self._poll_twitter_api(), name="twitter_api"),
                )
                tasks.append(
                    asyncio.create_task(self._poll_twitter_search(), name="twitter_search"),
                )
                logger.info("Twitter/X API polling enabled")
            else:
                logger.info("Twitter/X API polling disabled (no TWITTER_BEARER_TOKEN)")
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
            await asyncio.sleep(self._poll_interval * 2)

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

    # --- Nitter Instance Polling ---

    async def _poll_nitter(self) -> None:
        """Poll multiple Nitter instances in sequence for Trump posts.

        Nitter mirrors repost Truth Social content as RSS feeds.
        Tries each instance in order; on success, parses with _parse_rss().
        """
        while self._running:
            for nitter_url in self.NITTER_INSTANCES:
                try:
                    self._rotate_user_agent()
                    resp = await self._http.get(nitter_url)
                    if resp.status_code == 200:
                        posts = self._parse_rss(resp.text)
                        for post in posts:
                            # Re-tag source so we know it came from Nitter
                            post.source = "nitter"
                            if post.text_hash not in self._seen_hashes:
                                self._seen_hashes.add(post.text_hash)
                                await self._post_queue.put(post)
                                logger.info(
                                    "NEW POST detected (Nitter %s): %s... [latency=%dms]",
                                    nitter_url.split("/")[2],
                                    post.text[:80],
                                    post.detection_latency_ms,
                                )
                        # Got a successful response — no need to try more instances
                        break
                    else:
                        logger.debug(
                            "Nitter %s returned %d, trying next instance",
                            nitter_url.split("/")[2],
                            resp.status_code,
                        )
                except Exception as exc:
                    logger.debug(
                        "Nitter %s error: %s, trying next instance",
                        nitter_url.split("/")[2],
                        exc,
                    )

            await asyncio.sleep(self._poll_interval * 2)

    # --- Truth Social Atom Feed Polling ---

    async def _poll_truth_atom(self) -> None:
        """Poll the alternative Truth Social Atom/RSS endpoint."""
        atom_url = f"https://truthsocial.com/@{self.TRUMP_TS_USERNAME}.rss"

        while self._running:
            try:
                self._rotate_user_agent()
                resp = await self._http.get(atom_url)
                if resp.status_code == 200:
                    posts = self._parse_rss(resp.text)
                    for post in posts:
                        post.source = "truth_atom"
                        if post.text_hash not in self._seen_hashes:
                            self._seen_hashes.add(post.text_hash)
                            await self._post_queue.put(post)
                            logger.info(
                                "NEW POST detected (TruthAtom): %s... [latency=%dms]",
                                post.text[:80],
                                post.detection_latency_ms,
                            )
            except Exception as exc:
                logger.debug("Truth Atom poll error: %s", exc)

            await asyncio.sleep(self._poll_interval * 2)

    # --- Twitter/X API v2 Polling ---

    def _twitter_auth_headers(self) -> dict[str, str]:
        """Return Authorization header for Twitter API v2."""
        return {"Authorization": f"Bearer {config.TWITTER_BEARER_TOKEN}"}

    def _parse_twitter_tweet(self, tweet: dict) -> Optional[TrumpPost]:
        """Parse a Twitter API v2 tweet object into a TrumpPost."""
        try:
            text = tweet.get("text", "").strip()
            if not text:
                return None

            tweet_id = tweet.get("id", "")
            created_at = tweet.get("created_at", "")
            try:
                from datetime import datetime, timezone
                dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                ts = dt.timestamp()
            except (ValueError, TypeError):
                ts = time.time()

            return TrumpPost(
                post_id=str(tweet_id),
                text=text,
                timestamp=ts,
                source="twitter",
                url=f"https://x.com/realDonaldTrump/status/{tweet_id}" if tweet_id else "",
            )
        except Exception as exc:
            logger.error("Failed to parse Twitter tweet: %s", exc)
            return None

    async def _poll_twitter_api(self) -> None:
        """Poll the Twitter/X API v2 user timeline for new tweets."""
        user_id = config.TWITTER_TRUMP_USER_ID
        url = f"https://api.twitter.com/2/users/{user_id}/tweets"
        params = {
            "max_results": "5",
            "tweet.fields": "created_at,text",
        }

        while self._running:
            try:
                resp = await self._http.get(
                    url,
                    params=params,
                    headers=self._twitter_auth_headers(),
                )
                if resp.status_code == 200:
                    data = resp.json()
                    tweets = data.get("data", [])
                    for tweet in tweets:
                        post = self._parse_twitter_tweet(tweet)
                        if post and post.text_hash not in self._seen_hashes:
                            self._seen_hashes.add(post.text_hash)
                            await self._post_queue.put(post)
                            logger.info(
                                "NEW POST detected (Twitter API): %s... [latency=%dms]",
                                post.text[:80],
                                post.detection_latency_ms,
                            )
                elif resp.status_code == 429:
                    # Rate limited — read Retry-After or back off 60s
                    retry_after = int(resp.headers.get("Retry-After", "60"))
                    logger.warning(
                        "Twitter API rate limited (429), backing off %ds",
                        retry_after,
                    )
                    await asyncio.sleep(retry_after)
                    continue  # skip the normal sleep at the bottom
                elif resp.status_code == 401:
                    logger.error("Twitter API auth failed (401). Check TWITTER_BEARER_TOKEN.")
                else:
                    logger.debug("Twitter API returned %d", resp.status_code)

            except Exception as exc:
                logger.error("Twitter API poll error: %s", exc)

            await asyncio.sleep(self._poll_interval)

    async def _poll_twitter_search(self) -> None:
        """Poll the Twitter/X API v2 recent-search endpoint as a backup.

        Uses the search/recent endpoint with a from: query.  This catches
        tweets even if the user-timeline endpoint fails (e.g. suspended
        account, changed user ID, etc.).
        """
        url = "https://api.twitter.com/2/tweets/search/recent"
        params = {
            "query": "from:realDonaldTrump -is:retweet",
            "max_results": "10",
            "tweet.fields": "created_at,text",
        }

        while self._running:
            try:
                resp = await self._http.get(
                    url,
                    params=params,
                    headers=self._twitter_auth_headers(),
                )
                if resp.status_code == 200:
                    data = resp.json()
                    tweets = data.get("data", [])
                    for tweet in tweets:
                        post = self._parse_twitter_tweet(tweet)
                        if post:
                            # Re-tag so we can distinguish search hits in logs
                            post.source = "twitter_search"
                            if post.text_hash not in self._seen_hashes:
                                self._seen_hashes.add(post.text_hash)
                                await self._post_queue.put(post)
                                logger.info(
                                    "NEW POST detected (Twitter Search): %s... [latency=%dms]",
                                    post.text[:80],
                                    post.detection_latency_ms,
                                )
                elif resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", "60"))
                    logger.warning(
                        "Twitter Search rate limited (429), backing off %ds",
                        retry_after,
                    )
                    await asyncio.sleep(retry_after)
                    continue
                elif resp.status_code == 401:
                    logger.error("Twitter Search auth failed (401). Check TWITTER_BEARER_TOKEN.")
                else:
                    logger.debug("Twitter Search returned %d", resp.status_code)

            except Exception as exc:
                logger.error("Twitter Search poll error: %s", exc)

            # Search endpoint has stricter rate limits; poll a bit slower
            await asyncio.sleep(self._poll_interval * 2)

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
            # Simulate a post every 15-45 seconds in paper mode
            await asyncio.sleep(random.uniform(15, 45))
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
