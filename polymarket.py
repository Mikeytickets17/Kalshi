"""
Polymarket CLOB API wrapper.

Handles order placement, market data queries, and position management.
Exports Side, MarketInfo, OrderResult, Position for all other modules.
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


@dataclass
class MarketInfo:
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
    success: bool
    order_id: Optional[str] = None
    filled_price: Optional[float] = None
    filled_size: Optional[float] = None
    error: Optional[str] = None


@dataclass
class Position:
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
    """Polymarket CLOB API client."""

    def __init__(self) -> None:
        self._clob_url = config.POLYMARKET_CLOB_URL
        self._api_key = config.POLY_API_KEY
        self._http = httpx.Client(timeout=10.0)
        self._paper_mode = config.PAPER_MODE
        logger.info("PolymarketClient initialized (paper_mode=%s)", self._paper_mode)

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            h["Authorization"] = f"Bearer {self._api_key}"
        return h

    def get_market(self, condition_id: str) -> Optional[MarketInfo]:
        try:
            resp = self._http.get(
                f"{config.POLYMARKET_GAMMA_URL}/markets",
                params={"condition_id": condition_id},
            )
            resp.raise_for_status()
            data = resp.json()
            if not data:
                return None
            m = data[0] if isinstance(data, list) else data
            return MarketInfo(
                market_id=str(m.get("id", "")),
                ticker=str(m.get("ticker", "")),
                question=str(m.get("question", "")),
                category=str(m.get("category", "crypto")),
                yes_price=float(m.get("yes_price", 0.5)),
                no_price=float(m.get("no_price", 0.5)),
                liquidity_usdc=float(m.get("liquidity", 0)),
                volume_usdc=float(m.get("volume", 0)),
                end_date_ts=0,
                active=bool(m.get("active", False)),
                resolved=bool(m.get("resolved", False)),
            )
        except Exception as exc:
            logger.error("Failed to fetch market %s: %s", condition_id, exc)
            return None

    def place_order(
        self, token_id: str, side: Side, size_usdc: float, price: float,
        order_type: OrderType = OrderType.LIMIT,
    ) -> OrderResult:
        if self._paper_mode:
            return self._paper_fill(token_id, side, size_usdc, price)

        try:
            payload = {
                "tokenID": token_id,
                "side": "BUY",
                "size": str(size_usdc),
                "price": str(price),
                "type": order_type.value,
            }
            resp = self._http.post(
                f"{self._clob_url}/order", json=payload, headers=self._headers(),
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
            logger.error("Order failed: %s", exc)
            return OrderResult(success=False, error=str(exc))

    def cancel_order(self, order_id: str) -> bool:
        if self._paper_mode:
            return True
        try:
            self._http.delete(f"{self._clob_url}/order/{order_id}", headers=self._headers())
            return True
        except Exception:
            return False

    def cancel_all_orders(self) -> bool:
        if self._paper_mode:
            logger.info("[PAPER] Cancelled all orders")
            return True
        try:
            self._http.delete(f"{self._clob_url}/orders", headers=self._headers())
            return True
        except Exception:
            return False

    def _paper_fill(self, token_id: str, side: Side, size_usdc: float, price: float) -> OrderResult:
        order_id = f"paper-{int(time.time() * 1000)}"
        slippage = 0.002
        filled_price = round(max(0.01, min(0.99, price * (1 + slippage))), 4)
        logger.info(
            "[PAPER] Filled %s $%.2f @ %.4f (token=%s)",
            side.value, size_usdc, filled_price, token_id[:30],
        )
        return OrderResult(success=True, order_id=order_id, filled_price=filled_price, filled_size=size_usdc)

    def close(self) -> None:
        self._http.close()
        logger.info("PolymarketClient closed")
