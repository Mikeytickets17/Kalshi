"""KalshiAPI Protocol plus request/response types.

The executor consumes this interface. Real implementations wrap pykalshi;
tests use FakeKalshiAPI (see tests/fakes.py) to drive deterministic
scenarios without network calls.

Keeping this as a Protocol (structural type) means any new venue or a
fake just needs to match the shape -- no base class inheritance required.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class OrderRequest:
    market_ticker: str
    side: str                   # 'yes' | 'no'
    action: str                 # 'buy' | 'sell'
    order_type: str             # 'limit' | 'market'
    time_in_force: str          # 'IOC' | 'GTC'
    count: int
    limit_cents: int            # 0 for market orders
    client_order_id: str


@dataclass(frozen=True)
class OrderResponse:
    kalshi_order_id: str | None
    client_order_id: str
    filled_count: int
    requested_count: int
    avg_fill_price_cents: int   # 0 if unfilled
    fees_cents: int
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(frozen=True)
class PortfolioSnapshot:
    cash_cents: int
    positions: dict[str, int]   # ticker -> signed contract count
    at_ms: int


class KalshiAPI(Protocol):
    async def place_order(self, req: OrderRequest) -> OrderResponse: ...

    async def cancel_order(self, kalshi_order_id: str) -> None: ...

    async def get_portfolio(self) -> PortfolioSnapshot: ...
