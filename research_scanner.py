"""
Overnight Research Scanner.

Uses Brave Search API to continuously scan the internet for:
  1. New Kalshi/prediction market trading strategies
  2. Competitor approaches and alpha signals
  3. Trump policy moves and geopolitical developments
  4. Crypto regulation and market structure changes
  5. Top traders sharing strategies on social media

Sends a digest report via Telegram every morning at 7 AM ET,
and saves all findings to research_log.json on disk.

Usage:
    python research_scanner.py                 # Run continuous scanner
    python research_scanner.py --report-now    # Send report immediately
"""

import argparse
import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta

import httpx

import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

RESEARCH_LOG = os.path.join(os.path.dirname(__file__), "research_log.json")
BRAVE_API = "https://api.search.brave.com/res/v1"


# ═══ Search queries organized by category ═══

RESEARCH_QUERIES = {
    "kalshi_strategies": [
        "Kalshi trading strategy 2026",
        "Kalshi prediction market profitable strategy",
        "how to make money on Kalshi",
        "Kalshi arbitrage opportunity",
        "prediction market event contracts alpha",
        "Kalshi vs Polymarket trading edge",
        "best Kalshi contracts to trade",
    ],
    "trump_trades": [
        "Trump Truth Social market impact today",
        "Trump executive order prediction market",
        "Trump tariff announcement Kalshi",
        "Trump policy prediction contract",
        "Trump social media trading strategy",
    ],
    "geopolitical_alpha": [
        "Iran ceasefire prediction market odds",
        "Ukraine peace deal prediction contract",
        "NATO news prediction market",
        "geopolitical event trading strategy",
        "war headlines trading bot",
    ],
    "crypto_regulation": [
        "Bitcoin regulation prediction market",
        "crypto executive order Kalshi",
        "SEC crypto ruling prediction",
        "Bitcoin reserve policy odds",
        "stablecoin regulation prediction market",
    ],
    "market_structure": [
        "Kalshi API trading bot github",
        "prediction market automated trading",
        "Kalshi order flow analysis",
        "event contract market making strategy",
        "CFTC prediction market ruling",
    ],
    "competitor_intel": [
        "Polymarket whale tracker",
        "prediction market copy trading",
        "Kalshi top traders",
        "event contract hedge fund strategy",
        "prediction market institutional trading",
    ],
}


class ResearchScanner:
    """Scans the web for trading strategies and alpha signals."""

    def __init__(self) -> None:
        self._api_key = config.BRAVE_API_KEY
        self._http = httpx.AsyncClient(timeout=15.0)
        self._findings: list[dict] = []
        self._load_log()

    def _load_log(self) -> None:
        """Load existing research log from disk."""
        try:
            if os.path.exists(RESEARCH_LOG):
                with open(RESEARCH_LOG, "r") as f:
                    data = json.load(f)
                self._findings = data.get("findings", [])
                logger.info("Loaded %d existing research findings", len(self._findings))
        except Exception:
            self._findings = []

    def _save_log(self) -> None:
        """Save research log to disk."""
        try:
            data = {
                "last_updated": time.time(),
                "total_findings": len(self._findings),
                "findings": self._findings[-500:],  # Keep last 500
            }
            with open(RESEARCH_LOG, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as exc:
            logger.error("Failed to save research log: %s", exc)

    async def search(self, query: str, category: str) -> list[dict]:
        """Search Brave for a query and return findings."""
        if not self._api_key:
            logger.warning("No Brave API key — skipping search")
            return []

        results = []
        try:
            # Web search
            resp = await self._http.get(
                f"{BRAVE_API}/web/search",
                headers={
                    "Accept": "application/json",
                    "X-Subscription-Token": self._api_key,
                },
                params={"q": query, "count": 5, "freshness": "pw"},  # past week
            )
            if resp.status_code == 200:
                data = resp.json()
                for r in data.get("web", {}).get("results", []):
                    finding = {
                        "title": r.get("title", ""),
                        "url": r.get("url", ""),
                        "description": r.get("description", "")[:300],
                        "category": category,
                        "query": query,
                        "found_at": time.time(),
                        "source": "brave_web",
                    }
                    # Deduplicate by URL
                    if not any(f["url"] == finding["url"] for f in self._findings):
                        results.append(finding)

            # Also search news
            resp2 = await self._http.get(
                f"{BRAVE_API}/news/search",
                headers={
                    "Accept": "application/json",
                    "X-Subscription-Token": self._api_key,
                },
                params={"q": query, "count": 3, "freshness": "pd"},  # past day
            )
            if resp2.status_code == 200:
                data2 = resp2.json()
                for r in data2.get("results", []):
                    finding = {
                        "title": r.get("title", ""),
                        "url": r.get("url", ""),
                        "description": r.get("description", "")[:300],
                        "category": category,
                        "query": query,
                        "found_at": time.time(),
                        "source": "brave_news",
                    }
                    if not any(f["url"] == finding["url"] for f in self._findings):
                        results.append(finding)

        except Exception as exc:
            logger.debug("Search error for '%s': %s", query, exc)

        return results

    async def run_full_scan(self) -> int:
        """Run a complete scan across all categories. Returns number of new findings."""
        total_new = 0
        for category, queries in RESEARCH_QUERIES.items():
            for query in queries:
                findings = await self.search(query, category)
                if findings:
                    self._findings.extend(findings)
                    total_new += len(findings)
                    for f in findings:
                        logger.info("[%s] Found: %s", category, f["title"][:80])
                # Rate limit: Brave free tier = 1 req/sec
                await asyncio.sleep(1.5)

        self._save_log()
        logger.info("Scan complete: %d new findings, %d total", total_new, len(self._findings))
        return total_new

    def generate_report(self) -> str:
        """Generate a formatted research report for Telegram."""
        # Get findings from last 24 hours
        cutoff = time.time() - 86400
        recent = [f for f in self._findings if f.get("found_at", 0) > cutoff]

        if not recent:
            return "No new research findings in the last 24 hours."

        # Group by category
        by_cat: dict[str, list] = {}
        for f in recent:
            cat = f.get("category", "other")
            if cat not in by_cat:
                by_cat[cat] = []
            by_cat[cat].append(f)

        now = datetime.now(timezone(timedelta(hours=-4)))  # ET
        lines = [
            f"<b>Daily Research Report</b>",
            f"{now.strftime('%B %d, %Y %I:%M %p ET')}",
            f"{len(recent)} new findings\n",
        ]

        cat_labels = {
            "kalshi_strategies": "Kalshi Trading Strategies",
            "trump_trades": "Trump/Political Alpha",
            "geopolitical_alpha": "Geopolitical Opportunities",
            "crypto_regulation": "Crypto Regulation Intel",
            "market_structure": "Market Structure/Tools",
            "competitor_intel": "Competitor Intelligence",
        }

        for cat, items in by_cat.items():
            label = cat_labels.get(cat, cat.replace("_", " ").title())
            lines.append(f"\n<b>{label}</b> ({len(items)} items)")
            for item in items[:5]:  # Top 5 per category
                title = item["title"][:70]
                lines.append(f"  - {title}")
                if item.get("description"):
                    lines.append(f"    <i>{item['description'][:100]}...</i>")

        lines.append(f"\nFull log: research_log.json ({len(self._findings)} total)")
        return "\n".join(lines)

    async def send_telegram_report(self) -> bool:
        """Send the daily research report via Telegram."""
        if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
            logger.info("Telegram not configured — printing report to console")
            print(self.generate_report())
            return False

        report = self.generate_report()
        try:
            resp = await self._http.post(
                f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": config.TELEGRAM_CHAT_ID,
                    "text": report,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
            resp.raise_for_status()
            logger.info("Research report sent via Telegram")
            return True
        except Exception as exc:
            logger.error("Failed to send Telegram report: %s", exc)
            print(report)  # Fallback: print to console
            return False

    async def run_overnight(self) -> None:
        """Run continuous overnight research with morning report."""
        logger.info("Starting overnight research scanner")
        logger.info("Brave API key: %s...%s", self._api_key[:8], self._api_key[-4:] if self._api_key else "NONE")

        scan_count = 0
        while True:
            # Run a full scan
            scan_count += 1
            logger.info("Starting scan #%d", scan_count)
            new = await self.run_full_scan()
            logger.info("Scan #%d complete: %d new findings", scan_count, new)

            # Check if it's time for the morning report (7 AM ET)
            now_et = datetime.now(timezone(timedelta(hours=-4)))
            if now_et.hour == 7 and now_et.minute < 10:
                logger.info("Morning report time! Sending digest...")
                await self.send_telegram_report()
                await asyncio.sleep(3600)  # Don't send again for an hour

            # Wait 30 minutes between full scans
            logger.info("Next scan in 30 minutes...")
            await asyncio.sleep(1800)

    async def close(self) -> None:
        await self._http.aclose()


async def main(report_now: bool = False) -> None:
    scanner = ResearchScanner()

    if report_now:
        await scanner.run_full_scan()
        await scanner.send_telegram_report()
    else:
        try:
            await scanner.run_overnight()
        except KeyboardInterrupt:
            pass
        finally:
            scanner._save_log()
            await scanner.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Overnight research scanner")
    parser.add_argument("--report-now", action="store_true", help="Run one scan and send report immediately")
    args = parser.parse_args()
    asyncio.run(main(report_now=args.report_now))
