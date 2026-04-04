"""
Polymarket CLOB API wrapper.

Handles authentication, order placement, market data queries,
and position management via the Polymarket CLOB and Gamma APIs.
"""

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import httpx

import config

logger = logging.getLogger(__name__)


class Side(str, Enum):
    YES = "YES"
    NO = "NO"


class OrderType(str, Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"


@dataclass
class MarketInfo:
    """Snapshot of a Polymarket market."""
    market_id: str
    condition_id: str
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


class PolymarketClient:
    """Client for interacting with the Polymarket CLOB and Gamma APIs."""

    def __init__(self) -> None:
        self._clob_url = config.POLYMARKET_CLOB_URL
        self._gamma_url = config.POLYMARKET_GAMMA_URL
        self._api_key = config.POLY_API_KEY
        self._http = httpx.Client(timeout=30.0)
        self._paper_mode = config.PAPER_MODE
        logger.info("PolymarketClient initialized (paper_mode=%s)", self._paper_mode)

    def _clob_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    # --- Market Data ---

    def get_market(self, condition_id: str) -> Optional[MarketInfo]:
        """Fetch market info by condition_id from the Gamma API."""
        try:
            resp = self._http.get(
                f"{self._gamma_url}/markets",
                params={"condition_id": condition_id},
            )
            resp.raise_for_status()
            data = resp.json()
            if not data:
                return None
            m = data[0] if isinstance(data, list) else data
            return MarketInfo(
                market_id=str(m.get("id", "")),
                condition_id=str(m.get("condition_id", condition_id)),
                question=str(m.get("question", "")),
                category=str(m.get("category", "unknown")),
                yes_price=float(m.get("yes_price", 0.5)),
                no_price=float(m.get("no_price", 0.5)),
                liquidity_usdc=float(m.get("liquidity", 0)),
                volume_usdc=float(m.get("volume", 0)),
                end_date_ts=int(m.get("end_date_iso", 0) if isinstance(m.get("end_date_iso"), (int, float)) else 0),
                active=bool(m.get("active", False)),
                resolved=bool(m.get("resolved", False)),
            )
        except Exception as exc:
            logger.error("Failed to fetch market %s: %s", condition_id, exc)
            return None

    def get_market_by_id(self, market_id: str) -> Optional[MarketInfo]:
        """Fetch market info by market_id from the Gamma API."""
        try:
            resp = self._http.get(f"{self._gamma_url}/markets/{market_id}")
            resp.raise_for_status()
            m = resp.json()
            return MarketInfo(
                market_id=str(m.get("id", market_id)),
                condition_id=str(m.get("condition_id", "")),
                question=str(m.get("question", "")),
                category=str(m.get("category", "unknown")),
                yes_price=float(m.get("yes_price", 0.5)),
                no_price=float(m.get("no_price", 0.5)),
                liquidity_usdc=float(m.get("liquidity", 0)),
                volume_usdc=float(m.get("volume", 0)),
                end_date_ts=int(m.get("end_date_iso", 0) if isinstance(m.get("end_date_iso"), (int, float)) else 0),
                active=bool(m.get("active", False)),
                resolved=bool(m.get("resolved", False)),
            )
        except Exception as exc:
            logger.error("Failed to fetch market by id %s: %s", market_id, exc)
            return None

    def get_orderbook(self, token_id: str) -> dict:
        """Fetch order book for a given token_id."""
        try:
            resp = self._http.get(
                f"{self._clob_url}/book",
                params={"token_id": token_id},
                headers=self._clob_headers(),
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.error("Failed to fetch orderbook for %s: %s", token_id, exc)
            return {"bids": [], "asks": []}

    def get_price(self, token_id: str) -> Optional[float]:
        """Get the mid-price for a token from the order book."""
        try:
            resp = self._http.get(
                f"{self._clob_url}/midpoint",
                params={"token_id": token_id},
                headers=self._clob_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            return float(data.get("mid", 0.0))
        except Exception as exc:
            logger.error("Failed to fetch price for %s: %s", token_id, exc)
            return None

    # --- Order Management ---

    def place_order(
        self,
        token_id: str,
        side: Side,
        size_usdc: float,
        price: float,
        order_type: OrderType = OrderType.LIMIT,
    ) -> OrderResult:
        """Place an order on the Polymarket CLOB."""
        if self._paper_mode:
            return self._paper_fill(token_id, side, size_usdc, price)

        try:
            payload = {
                "tokenID": token_id,
                "side": "BUY" if side == Side.YES else "SELL",
                "size": str(size_usdc),
                "price": str(price),
                "type": order_type.value,
            }
            resp = self._http.post(
                f"{self._clob_url}/order",
                json=payload,
                headers=self._clob_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            return OrderResult(
                success=True,
                order_id=data.get("orderID"),
                filled_price=float(data.get("filledPrice", price)),
                filled_size=float(data.get("filledSize", size_usdc)),
            )
        except Exception as exc:
            logger.error("Order placement failed: %s", exc)
            return OrderResult(success=False, error=str(exc))

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order."""
        if self._paper_mode:
            logger.info("[PAPER] Cancelled order %s", order_id)
            return True
        try:
            resp = self._http.delete(
                f"{self._clob_url}/order/{order_id}",
                headers=self._clob_headers(),
            )
            resp.raise_for_status()
            return True
        except Exception as exc:
            logger.error("Failed to cancel order %s: %s", order_id, exc)
            return False

    def cancel_all_orders(self) -> bool:
        """Cancel all open orders."""
        if self._paper_mode:
            logger.info("[PAPER] Cancelled all orders")
            return True
        try:
            resp = self._http.delete(
                f"{self._clob_url}/orders",
                headers=self._clob_headers(),
            )
            resp.raise_for_status()
            return True
        except Exception as exc:
            logger.error("Failed to cancel all orders: %s", exc)
            return False

    # --- Paper Mode ---

    def _paper_fill(
        self, token_id: str, side: Side, size_usdc: float, price: float
    ) -> OrderResult:
        """Simulate an order fill in paper mode."""
        order_id = f"paper-{int(time.time() * 1000)}"
        slippage = 0.002
        # Slippage always costs us — we pay more for YES, pay more for NO
        filled_price = price * (1 + slippage) if side == Side.YES else price * (1 + slippage)
        filled_price = round(max(0.01, min(0.99, filled_price)), 4)
        logger.info(
            "[PAPER] Filled %s %s %.2f USDC @ %.4f (token=%s, order=%s)",
            side.value, "BUY", size_usdc, filled_price, token_id, order_id,
        )
        return OrderResult(
            success=True,
            order_id=order_id,
            filled_price=filled_price,
            filled_size=size_usdc,
        )

    # --- Cleanup ---

    def close(self) -> None:
        """Close the HTTP client."""
        self._http.close()
        logger.info("PolymarketClient closed")
