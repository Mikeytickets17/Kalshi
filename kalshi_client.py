"""
Kalshi API client for latency arbitrage.

Handles authentication, market scanning for crypto contracts,
and limit order placement on Kalshi's regulated exchange.

US-legal from all 50 states including New Jersey.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import config

logger = logging.getLogger(__name__)


@dataclass
class KalshiMarket:
    """A Kalshi market (crypto price bracket contract)."""
    ticker: str
    title: str
    category: str
    yes_price: float
    no_price: float
    volume: float
    close_time_ts: int
    asset: str           # BTC, ETH
    strike: float        # price level
    direction: str       # "above" or "below"
    active: bool
    settled: bool


@dataclass
class KalshiOrder:
    """Result of placing an order on Kalshi."""
    success: bool
    order_id: str = ""
    filled_price: float = 0.0
    filled_size: float = 0.0
    error: str = ""


class KalshiClient:
    """Client for Kalshi's REST API via pykalshi."""

    def __init__(self) -> None:
        self._use_demo = config.KALSHI_USE_DEMO
        self._api_key_id = config.KALSHI_API_KEY_ID
        self._private_key_path = config.KALSHI_PRIVATE_KEY_PATH
        self._paper_mode = config.PAPER_MODE
        self._client: Any = None
        self._base_url = config.KALSHI_BASE_URL_DEMO if self._use_demo else config.KALSHI_BASE_URL_PROD

        self._init_client()
        logger.info(
            "KalshiClient initialized (demo=%s, paper=%s)",
            self._use_demo, self._paper_mode,
        )

    def _init_client(self) -> None:
        if not self._api_key_id or not self._private_key_path:
            logger.warning("Kalshi API credentials not configured — paper mode only")
            return
        try:
            from pykalshi import HttpClient
            with open(self._private_key_path, "r") as f:
                private_key = f.read()
            self._client = HttpClient(
                key_id=self._api_key_id,
                private_key=private_key,
                base_url=self._base_url,
            )
            logger.info("Kalshi API authenticated")
        except ImportError:
            logger.warning("pykalshi not installed, paper mode only")
        except Exception as exc:
            logger.error("Kalshi auth failed: %s", exc)

    @property
    def is_connected(self) -> bool:
        return self._client is not None

    def get_crypto_markets(self) -> list[KalshiMarket]:
        """Fetch open crypto price contracts from Kalshi."""
        if not self._client:
            return []
        try:
            resp = self._client.get_markets(status="open", limit=200)
            markets = []
            for m in resp.get("markets", []):
                ticker = m.get("ticker", "")
                title = (m.get("title", "") or "").lower()
                # Filter for crypto price contracts
                if not any(kw in title for kw in ["bitcoin", "btc", "ethereum", "eth", "crypto"]):
                    continue
                market = self._parse_market(m)
                if market:
                    markets.append(market)
            return markets
        except Exception as exc:
            logger.error("Failed to fetch Kalshi markets: %s", exc)
            return []

    def _parse_market(self, m: dict) -> Optional[KalshiMarket]:
        try:
            yes_ask = m.get("yes_ask")
            yes_price = yes_ask if yes_ask is not None else (m.get("last_price") or 50)
            if isinstance(yes_price, (int, float)) and yes_price > 1:
                yes_price = yes_price / 100.0

            close_time = m.get("close_time", "") or m.get("expiration_time", "")
            end_ts = 0
            if close_time:
                try:
                    from datetime import datetime
                    dt = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
                    end_ts = int(dt.timestamp())
                except (ValueError, TypeError):
                    pass

            title = m.get("title", "") or ""
            ticker = m.get("ticker", "")

            # Detect asset and strike from title
            asset = "BTC" if "btc" in title.lower() or "bitcoin" in title.lower() else "ETH"
            strike = self._extract_strike(title)
            direction = "above" if "above" in title.lower() or "over" in title.lower() else "below"

            return KalshiMarket(
                ticker=ticker,
                title=title,
                category="crypto",
                yes_price=round(yes_price, 4),
                no_price=round(1.0 - yes_price, 4),
                volume=float(m.get("volume", 0) or 0),
                close_time_ts=end_ts,
                asset=asset,
                strike=strike,
                direction=direction,
                active=m.get("status", "") in ("open", "active", ""),
                settled=m.get("result", "") not in ("", None),
            )
        except Exception as exc:
            logger.debug("Failed to parse Kalshi market: %s", exc)
            return None

    def _extract_strike(self, title: str) -> float:
        """Extract the price level from a contract title."""
        import re
        # Match patterns like "$68,500" or "68500" or "$68.5k"
        patterns = [
            r"\$?([\d,]+(?:\.\d+)?)\s*(?:k|K)",
            r"\$?([\d,]+(?:\.\d+)?)",
        ]
        for pat in patterns:
            match = re.search(pat, title)
            if match:
                val = match.group(1).replace(",", "")
                num = float(val)
                if "k" in title.lower() and num < 1000:
                    num *= 1000
                if num > 100:  # Looks like a price, not a percentage
                    return num
        return 0.0

    def place_order(
        self, ticker: str, side: str, size_usd: float, price: float,
    ) -> KalshiOrder:
        """Place a limit order on Kalshi. Always limit, never market."""
        if self._paper_mode:
            return self._paper_fill(ticker, side, size_usd, price)

        if not self._client:
            return KalshiOrder(success=False, error="Not connected")

        try:
            price_cents = max(1, min(99, int(round(price * 100))))
            # Each contract costs price_cents. size_usd is in dollars.
            # cost_per_contract = price_cents / 100
            count = max(1, int(size_usd / (price_cents / 100)))

            params: dict[str, Any] = {
                "ticker": ticker,
                "action": "buy",
                "side": side.lower(),
                "type": "limit",
                "count": count,
            }
            if side.upper() == "YES":
                params["yes_price"] = price_cents
            else:
                params["no_price"] = price_cents

            resp = self._client.create_order(**params)
            order = resp.get("order", resp)

            return KalshiOrder(
                success=True,
                order_id=order.get("order_id", ""),
                filled_price=price,
                filled_size=size_usd,
            )
        except Exception as exc:
            logger.error("Kalshi order failed: %s", exc)
            return KalshiOrder(success=False, error=str(exc))

    def get_market(self, ticker: str) -> Optional[dict]:
        """Fetch current state of a specific market by ticker."""
        if not self._client:
            return None
        try:
            resp = self._client.get_market(ticker)
            return resp.get("market", resp)
        except Exception as exc:
            logger.error("Failed to get market %s: %s", ticker, exc)
            return None

    def get_market_price(self, ticker: str) -> Optional[float]:
        """Get the current YES price for a market."""
        market = self.get_market(ticker)
        if not market:
            return None
        yes_ask = market.get("yes_ask")
        yes_price = yes_ask if yes_ask is not None else market.get("last_price")
        if yes_price is None:
            return None
        if isinstance(yes_price, (int, float)) and yes_price > 1:
            yes_price = yes_price / 100.0
        return round(float(yes_price), 4)

    def check_settlement(self, ticker: str) -> tuple[bool, Optional[str]]:
        """
        Check if a Kalshi contract has settled.

        Returns:
            (is_settled, result) where result is "yes", "no", or None if not settled.
        """
        market = self.get_market(ticker)
        if not market:
            return False, None

        status = market.get("status", "")
        result = market.get("result", "")

        if status in ("settled", "finalized", "closed") and result:
            return True, result.lower()
        if result and result.lower() in ("yes", "no"):
            return True, result.lower()

        return False, None

    def cancel_all(self) -> None:
        if self._paper_mode:
            logger.info("[PAPER] Cancelled all Kalshi orders")
            return
        logger.info("Requested cancel all Kalshi orders")

    def _paper_fill(self, ticker: str, side: str, size_usd: float, price: float) -> KalshiOrder:
        slippage = 0.003
        filled = round(max(0.01, min(0.99, price * (1 + slippage))), 4)
        logger.info(
            "[PAPER] Kalshi filled %s $%.2f @ %.4f (ticker=%s)",
            side, size_usd, filled, ticker[:30],
        )
        return KalshiOrder(
            success=True,
            order_id=f"paper-{int(time.time()*1000)}",
            filled_price=filled,
            filled_size=size_usd,
        )

    def close(self) -> None:
        self._client = None
        logger.info("KalshiClient closed")
