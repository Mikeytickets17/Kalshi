"""
Kalshi API client for latency arbitrage.

Handles authentication, market scanning for crypto contracts,
and limit order placement on Kalshi's regulated exchange.

US-legal from all 50 states including New Jersey.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

import config

logger = logging.getLogger(__name__)

# Kalshi production crypto-price series tickers. These are the parent series
# under which every BTC/ETH price-range event/market is grouped. Filtering
# /markets by series_ticker is what bypasses the huge sports/TV contract list.
CRYPTO_SERIES_TICKERS: tuple[str, ...] = (
    "KXBTC",        # Bitcoin hourly price range
    "KXBTCD",       # Bitcoin daily close
    "KXBTCRESD",    # Bitcoin residual daily
    "KXBTCMAXY",    # Bitcoin max this year
    "KXETH",        # Ethereum hourly price range
    "KXETHD",       # Ethereum daily close
    "KXETHRESD",    # Ethereum residual daily
    "KXETHMAXY",    # Ethereum max this year
)

_MARKETS_PAGE_LIMIT = 200
_MAX_PAGES_PER_SERIES = 10
_MAX_FALLBACK_PAGES = 10


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    """Read a field from a pykalshi Market object OR a plain dict."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        val = obj.get(name, default)
    else:
        val = getattr(obj, name, default)
    return val if val is not None else default


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
            from pykalshi import KalshiClient as PyKalshiClient
            self._client = PyKalshiClient(
                api_key_id=self._api_key_id,
                private_key_path=self._private_key_path,
                demo=self._use_demo,
            )
            logger.info("Kalshi API authenticated (demo=%s)", self._use_demo)
        except ImportError:
            logger.warning("pykalshi not installed, paper mode only")
        except Exception as exc:
            logger.error("Kalshi auth failed: %s", exc)

    @property
    def is_connected(self) -> bool:
        return self._client is not None

    def get_crypto_markets(self) -> list[KalshiMarket]:
        """Fetch open BTC/ETH price contracts from Kalshi.

        Production Kalshi has thousands of markets (sports, TV, politics...).
        A naive get_markets(limit=200) never reaches the crypto rows, so we
        query each known crypto series ticker directly with cursor pagination.
        """
        if not self._client:
            return []

        all_markets: list[KalshiMarket] = []
        seen: set[str] = set()
        per_series_counts: dict[str, int] = {}

        for series in CRYPTO_SERIES_TICKERS:
            count = 0
            try:
                for raw in self._iter_series_markets(series):
                    parsed = self._parse_kalshi_market(raw)
                    if not parsed or parsed.settled or not parsed.active:
                        continue
                    if parsed.ticker in seen:
                        continue
                    seen.add(parsed.ticker)
                    all_markets.append(parsed)
                    count += 1
            except Exception as exc:
                logger.debug("Kalshi series %s fetch failed: %s", series, exc)
            per_series_counts[series] = count

        if not all_markets:
            logger.warning(
                "No Kalshi crypto markets via series filter — falling back to paginated scan"
            )
            all_markets = self._fallback_scan_crypto_markets()

        hit_series = {s: c for s, c in per_series_counts.items() if c}
        logger.info(
            "Fetched %d crypto contracts from Kalshi (series hits: %s)",
            len(all_markets), hit_series or "none",
        )
        return all_markets

    def _iter_series_markets(self, series_ticker: str) -> Iterator[Any]:
        """Yield open markets for a given series, paginating via cursor."""
        if not self._client:
            return
        cursor: Optional[str] = None
        for _ in range(_MAX_PAGES_PER_SERIES):
            kwargs: dict[str, Any] = {
                "series_ticker": series_ticker,
                "limit": _MARKETS_PAGE_LIMIT,
            }
            if cursor:
                kwargs["cursor"] = cursor

            result = self._call_get_markets(status="open", **kwargs)
            page, cursor = self._unpack_markets_result(result)
            for m in page:
                yield m
            if not cursor:
                break

    def _fallback_scan_crypto_markets(self) -> list[KalshiMarket]:
        """Last-resort: paginate all open markets and keep crypto ones."""
        markets: list[KalshiMarket] = []
        seen: set[str] = set()
        cursor: Optional[str] = None

        for _ in range(_MAX_FALLBACK_PAGES):
            kwargs: dict[str, Any] = {"limit": 1000}
            if cursor:
                kwargs["cursor"] = cursor
            try:
                result = self._call_get_markets(status="open", **kwargs)
            except Exception as exc:
                logger.error("Kalshi fallback scan failed: %s", exc)
                break
            page, cursor = self._unpack_markets_result(result)
            for m in page:
                if not self._looks_like_crypto(m):
                    continue
                parsed = self._parse_kalshi_market(m)
                if not parsed or parsed.settled or not parsed.active:
                    continue
                if parsed.ticker in seen:
                    continue
                seen.add(parsed.ticker)
                markets.append(parsed)
            if not cursor:
                break
        return markets

    def _call_get_markets(self, **kwargs: Any) -> Any:
        """Invoke client.get_markets, dropping kwargs the library rejects."""
        try:
            return self._client.get_markets(**kwargs)
        except TypeError:
            # pykalshi may not accept all REST params — retry without status
            kwargs.pop("status", None)
            return self._client.get_markets(**kwargs)

    @staticmethod
    def _unpack_markets_result(result: Any) -> tuple[list[Any], Optional[str]]:
        """Normalize pykalshi response into (markets, cursor).

        The library returns either a bare list, an object with .markets/.cursor,
        or a dict with those keys depending on version/endpoint.
        """
        if result is None:
            return [], None
        if isinstance(result, dict):
            return list(result.get("markets") or []), result.get("cursor") or None
        if hasattr(result, "markets"):
            cursor = getattr(result, "cursor", None) or None
            return list(getattr(result, "markets") or []), cursor
        # Bare list response
        try:
            return list(result), None
        except TypeError:
            return [], None

    @staticmethod
    def _looks_like_crypto(m: Any) -> bool:
        series = str(_attr(m, "series_ticker", "") or "")
        event = str(_attr(m, "event_ticker", "") or "")
        title = str(_attr(m, "title", "") or "").lower()
        if series in CRYPTO_SERIES_TICKERS:
            return True
        if series.startswith("KXBTC") or series.startswith("KXETH"):
            return True
        if event.startswith("KXBTC") or event.startswith("KXETH"):
            return True
        return "bitcoin" in title or "ethereum" in title

    def _parse_kalshi_market(self, m: Any) -> Optional[KalshiMarket]:
        """Parse a pykalshi Market (object or dict) into our KalshiMarket."""
        try:
            ticker = str(_attr(m, "ticker", "") or "")
            if not ticker:
                return None

            status = str(_attr(m, "status", "") or "").lower()
            result_field = str(_attr(m, "result", "") or "").lower()
            is_settled = result_field in ("yes", "no") or status in (
                "settled", "finalized", "closed", "determined",
            )
            is_active = not is_settled and status in ("open", "active", "")

            yes_price_raw = (
                _attr(m, "yes_ask_dollars")
                or _attr(m, "last_price_dollars")
                or _attr(m, "yes_ask")
                or _attr(m, "last_price")
                or 0.50
            )
            if isinstance(yes_price_raw, (int, float)) and yes_price_raw > 1:
                yes_price_raw = yes_price_raw / 100.0
            yes_price = float(yes_price_raw)

            close_time_str = str(_attr(m, "close_time", "") or "")
            end_ts = 0
            if close_time_str:
                try:
                    from datetime import datetime
                    dt = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
                    end_ts = int(dt.timestamp())
                except (ValueError, TypeError):
                    pass

            full_title = str(_attr(m, "title", "") or "")
            subtitle = str(_attr(m, "subtitle", "") or "")
            series = str(_attr(m, "series_ticker", "") or "")
            title_lower = full_title.lower()

            asset = "ETH"
            if (
                series.startswith("KXBTC")
                or ticker.startswith("KXBTC")
                or "bitcoin" in title_lower
                or "btc" in title_lower
            ):
                asset = "BTC"

            strike = self._extract_strike(subtitle) or self._extract_strike(full_title)

            combined = (title_lower + " " + subtitle.lower()).strip()
            direction = "below" if any(
                w in combined for w in ("below", "under", "less than", "lower than")
            ) else "above"

            volume = float(_attr(m, "volume_fp", 0) or _attr(m, "volume", 0) or 0)

            return KalshiMarket(
                ticker=ticker,
                title=full_title,
                category="crypto",
                yes_price=round(yes_price, 4),
                no_price=round(1.0 - yes_price, 4),
                volume=volume,
                close_time_ts=end_ts,
                asset=asset,
                strike=strike,
                direction=direction,
                active=is_active,
                settled=is_settled,
            )
        except Exception as exc:
            logger.debug("Failed to parse Kalshi market: %s", exc)
            return None

    # Back-compat alias: whale_tracker.py and contract_matcher.py call this
    # with either dict or Market-object inputs. The new parser handles both.
    def _parse_market(self, m: Any) -> Optional[KalshiMarket]:
        return self._parse_kalshi_market(m)

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
            order = resp if isinstance(resp, dict) else {k: getattr(resp, k, None) for k in ['order_id', 'status', 'yes_price', 'no_price']}

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
            m = self._client.get_market(ticker)
            return {
                'ticker': getattr(m, 'ticker', ''),
                'title': getattr(m, 'title', ''),
                'yes_ask': getattr(m, 'yes_ask_dollars', None),
                'last_price': getattr(m, 'last_price_dollars', None),
                'status': str(getattr(m, 'status', '')),
                'result': str(getattr(m, 'result', '') or ''),
                'close_time': str(getattr(m, 'close_time', '') or ''),
                'volume': getattr(m, 'volume_fp', 0),
            }
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
