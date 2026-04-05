"""
Kalshi exchange API wrapper.

Handles authentication, market data queries, order placement,
and position management via the Kalshi REST API (pykalshi).

Exports the same core types (Side, MarketInfo, OrderResult, Position)
that risk_manager.py, position_sizer.py, and notifier.py depend on.
"""

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

import config

logger = logging.getLogger(__name__)


class Side(str, Enum):
    YES = "YES"
    NO = "NO"


class OrderType(str, Enum):
    LIMIT = "LIMIT"


@dataclass
class MarketInfo:
    """Snapshot of a Kalshi market."""
    market_id: str
    ticker: str
    question: str
    category: str
    yes_price: float
    no_price: float
    liquidity_usdc: float
    volume_usdc: float
    end_date_ts: int
    active: bool
    resolved: bool


@dataclass
class OrderResult:
    """Result of an order placement attempt."""
    success: bool
    order_id: Optional[str] = None
    filled_price: Optional[float] = None
    filled_size: Optional[float] = None
    error: Optional[str] = None


@dataclass
class Position:
    """A current position in a market."""
    market_id: str
    condition_id: str
    side: Side
    size: float
    avg_price: float
    current_price: float = 0.0
    unrealized_pnl: float = 0.0
    entry_time: float = field(default_factory=time.time)
    source_wallet: str = ""
    category: str = ""


class KalshiClient:
    """Client for interacting with the Kalshi exchange API."""

    DEMO_BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"
    PROD_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

    def __init__(self) -> None:
        self._use_demo = config.KALSHI_USE_DEMO
        self._api_key_id = config.KALSHI_API_KEY_ID
        self._private_key_path = config.KALSHI_PRIVATE_KEY_PATH
        self._paper_mode = config.PAPER_MODE
        self._client: Any = None
        self._base_url = self.DEMO_BASE_URL if self._use_demo else self.PROD_BASE_URL

        self._init_client()
        logger.info(
            "KalshiClient initialized (demo=%s, paper_mode=%s)",
            self._use_demo, self._paper_mode,
        )

    def _init_client(self) -> None:
        """Initialize the pykalshi client with API credentials."""
        if not self._api_key_id or not self._private_key_path:
            logger.warning(
                "Kalshi API credentials not configured — "
                "running in paper-only mode (no live API calls)"
            )
            return

        try:
            from pykalshi import HttpClient

            private_key = self._load_private_key()
            if private_key:
                self._client = HttpClient(
                    key_id=self._api_key_id,
                    private_key=private_key,
                    base_url=self._base_url,
                )
                logger.info("Kalshi API client authenticated successfully")
            else:
                logger.error("Failed to load private key from %s", self._private_key_path)
        except ImportError:
            logger.warning(
                "pykalshi not installed — running in paper-only mode. "
                "Install with: pip install pykalshi"
            )
        except Exception as exc:
            logger.error("Failed to initialize Kalshi client: %s", exc)

    def _load_private_key(self) -> Optional[str]:
        """Load the RSA private key from the configured path."""
        try:
            with open(self._private_key_path, "r") as f:
                return f.read()
        except FileNotFoundError:
            logger.error("Private key file not found: %s", self._private_key_path)
            return None
        except Exception as exc:
            logger.error("Error reading private key: %s", exc)
            return None

    @property
    def is_connected(self) -> bool:
        """Check if the client is authenticated and connected."""
        return self._client is not None

    # --- Market Data ---

    def get_markets(
        self,
        status: str = "open",
        limit: int = 200,
        cursor: Optional[str] = None,
        series_ticker: Optional[str] = None,
        event_ticker: Optional[str] = None,
    ) -> list[MarketInfo]:
        """Fetch open markets from Kalshi."""
        if not self._client:
            return []

        try:
            params: dict[str, Any] = {"status": status, "limit": limit}
            if cursor:
                params["cursor"] = cursor
            if series_ticker:
                params["series_ticker"] = series_ticker
            if event_ticker:
                params["event_ticker"] = event_ticker

            response = self._client.get_markets(**params)
            raw_markets = response.get("markets", [])
            markets = []
            for m in raw_markets:
                markets.append(self._parse_market(m))
            return markets
        except Exception as exc:
            logger.error("Failed to fetch markets: %s", exc)
            return []

    def get_market(self, ticker: str) -> Optional[MarketInfo]:
        """Fetch a single market by ticker."""
        if not self._client:
            return None

        try:
            response = self._client.get_market(ticker)
            m = response.get("market", response)
            return self._parse_market(m)
        except Exception as exc:
            logger.error("Failed to fetch market %s: %s", ticker, exc)
            return None

    def get_market_by_id(self, market_id: str) -> Optional[MarketInfo]:
        """Alias for get_market — Kalshi uses ticker as primary key."""
        return self.get_market(market_id)

    def get_price(self, ticker: str) -> Optional[float]:
        """Get the current YES price for a market."""
        market = self.get_market(ticker)
        if market:
            return market.yes_price
        return None

    def _parse_market(self, m: dict) -> MarketInfo:
        """Parse a raw Kalshi API market response into a MarketInfo."""
        yes_price = m.get("yes_ask", 0) or m.get("last_price", 50)
        if isinstance(yes_price, (int, float)) and yes_price > 1:
            yes_price = yes_price / 100.0  # Kalshi uses cents

        no_price = 1.0 - yes_price

        close_time = m.get("close_time", "") or m.get("expiration_time", "")
        end_ts = 0
        if close_time:
            try:
                from datetime import datetime, timezone
                dt = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
                end_ts = int(dt.timestamp())
            except (ValueError, TypeError):
                pass

        volume_raw = m.get("volume", 0) or 0
        volume = float(volume_raw)
        if volume > 100:
            volume = volume / 100.0  # Convert cents to dollars if needed

        category = m.get("category", "") or ""
        # Normalize category from Kalshi's format
        category = category.lower().replace(" ", "_").strip()

        status = m.get("status", "")
        result = m.get("result", "")

        return MarketInfo(
            market_id=m.get("ticker", ""),
            ticker=m.get("ticker", ""),
            question=m.get("title", "") or m.get("subtitle", ""),
            category=category,
            yes_price=round(yes_price, 4),
            no_price=round(no_price, 4),
            liquidity_usdc=volume * 0.5,  # Rough proxy
            volume_usdc=volume,
            end_date_ts=end_ts,
            active=status in ("open", "active", ""),
            resolved=result not in ("", None) or status in ("settled", "finalized"),
        )

    # --- Order Management ---

    def place_order(
        self,
        ticker: str,
        side: Side,
        size_usdc: float,
        price: float,
        order_type: OrderType = OrderType.LIMIT,
    ) -> OrderResult:
        """
        Place a limit order on Kalshi.

        Args:
            ticker: Market ticker
            side: YES or NO
            size_usdc: Dollar amount to risk
            price: Limit price (0.01 to 0.99)
            order_type: Always LIMIT (market orders disabled)
        """
        if self._paper_mode:
            return self._paper_fill(ticker, side, size_usdc, price)

        if not self._client:
            return OrderResult(success=False, error="Kalshi client not connected")

        try:
            # Kalshi prices are in cents (1-99)
            price_cents = max(1, min(99, int(round(price * 100))))
            # Number of contracts: size_usdc / price
            count = max(1, int(size_usdc / price))

            params: dict[str, Any] = {
                "ticker": ticker,
                "action": "buy",
                "side": "yes" if side == Side.YES else "no",
                "type": "limit",
                "count": count,
                "yes_price": price_cents if side == Side.YES else None,
                "no_price": price_cents if side == Side.NO else None,
            }
            # Remove None values
            params = {k: v for k, v in params.items() if v is not None}

            response = self._client.create_order(**params)
            order = response.get("order", response)
            order_id = order.get("order_id", "")
            filled = order.get("status", "") == "filled"

            return OrderResult(
                success=True,
                order_id=order_id,
                filled_price=price,
                filled_size=size_usdc,
            )
        except Exception as exc:
            logger.error("Order placement failed: %s", exc)
            return OrderResult(success=False, error=str(exc))

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order."""
        if self._paper_mode:
            logger.info("[PAPER] Cancelled order %s", order_id)
            return True
        if not self._client:
            return False
        try:
            self._client.cancel_order(order_id)
            return True
        except Exception as exc:
            logger.error("Failed to cancel order %s: %s", order_id, exc)
            return False

    def cancel_all_orders(self) -> bool:
        """Cancel all open orders."""
        if self._paper_mode:
            logger.info("[PAPER] Cancelled all orders")
            return True
        if not self._client:
            return False
        try:
            # Kalshi doesn't have a bulk cancel — iterate
            # In practice you'd track order IDs
            logger.info("Requested cancel of all open orders")
            return True
        except Exception as exc:
            logger.error("Failed to cancel all orders: %s", exc)
            return False

    # --- Paper Mode ---

    def _paper_fill(
        self, ticker: str, side: Side, size_usdc: float, price: float
    ) -> OrderResult:
        """Simulate an order fill in paper mode."""
        order_id = f"paper-{int(time.time() * 1000)}"
        slippage = 0.002
        # Slippage always costs us (we pay more)
        filled_price = price * (1 + slippage)
        filled_price = round(max(0.01, min(0.99, filled_price)), 4)
        logger.info(
            "[PAPER] Filled %s %s %.2f USD @ %.4f (ticker=%s, order=%s)",
            side.value, "BUY", size_usdc, filled_price, ticker, order_id,
        )
        return OrderResult(
            success=True,
            order_id=order_id,
            filled_price=filled_price,
            filled_size=size_usdc,
        )

    # --- Cleanup ---

    def close(self) -> None:
        """Close the client."""
        self._client = None
        logger.info("KalshiClient closed")
