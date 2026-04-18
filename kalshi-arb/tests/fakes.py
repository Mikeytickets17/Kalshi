"""Test fakes. Zero network. Deterministic. Used by executor + pipeline tests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Callable

from kalshi_arb.executor.kalshi_api import (
    OrderRequest,
    OrderResponse,
    PortfolioSnapshot,
)


# A fill policy is a function that maps an OrderRequest -> OrderResponse.
# Tests construct these to simulate: both fill, one fills, both reject,
# unwind timeout, unwind partial, etc.
FillPolicy = Callable[[OrderRequest], OrderResponse]


@dataclass
class FakeKalshiAPI:
    """Deterministic stand-in for Kalshi's REST + WS auth layer.

    - `fill_policy` decides how each place_order call resolves.
    - `place_delay_sec` lets tests exercise asyncio.gather concurrency.
    - `portfolio_reads` is a list of PortfolioSnapshot values the monitor
      will see in order (each get_portfolio() call pops one).
    - Every order is recorded in `placed_orders` for assertions.
    - Duplicate `client_order_id` is server-side deduped: the second call
      returns the same OrderResponse as the first (this is what real
      Kalshi does and is how idempotency is enforced).
    """

    fill_policy: FillPolicy
    place_delay_sec: float = 0.0
    unwind_delay_sec: float | None = None  # None -> same as place_delay_sec
    portfolio_reads: list[PortfolioSnapshot] = field(default_factory=list)
    placed_orders: list[OrderRequest] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)
    _dedupe_cache: dict[str, OrderResponse] = field(default_factory=dict)

    async def place_order(self, req: OrderRequest) -> OrderResponse:
        # Idempotency: if we've seen this client_order_id before, return the
        # prior response verbatim -- matches Kalshi's real behavior.
        if req.client_order_id in self._dedupe_cache:
            return self._dedupe_cache[req.client_order_id]

        delay = (
            self.unwind_delay_sec
            if (self.unwind_delay_sec is not None and req.order_type == "market")
            else self.place_delay_sec
        )
        if delay > 0:
            await asyncio.sleep(delay)
        self.placed_orders.append(req)
        resp = self.fill_policy(req)
        self._dedupe_cache[req.client_order_id] = resp
        return resp

    async def cancel_order(self, kalshi_order_id: str) -> None:
        self.cancelled.append(kalshi_order_id)

    async def get_portfolio(self) -> PortfolioSnapshot:
        if not self.portfolio_reads:
            return PortfolioSnapshot(cash_cents=0, positions={}, at_ms=0)
        return self.portfolio_reads.pop(0)


# ---------- Common fill policies ----------


def policy_both_fill_fully(req: OrderRequest) -> OrderResponse:
    return OrderResponse(
        kalshi_order_id=f"kalshi-{req.client_order_id[-8:]}",
        client_order_id=req.client_order_id,
        filled_count=req.count,
        requested_count=req.count,
        avg_fill_price_cents=req.limit_cents if req.limit_cents > 0 else 50,
        fees_cents=2 * req.count,  # approx -- tests don't check exact fees
    )


def policy_both_reject(req: OrderRequest) -> OrderResponse:
    return OrderResponse(
        kalshi_order_id=None,
        client_order_id=req.client_order_id,
        filled_count=0,
        requested_count=req.count,
        avg_fill_price_cents=0,
        fees_cents=0,
        error="ioc_no_fill",
    )


def policy_yes_fills_no_rejects(req: OrderRequest) -> OrderResponse:
    if req.side == "yes" and req.action == "buy":
        return policy_both_fill_fully(req)
    if req.side == "no" and req.action == "buy":
        return policy_both_reject(req)
    # Unwind (sell) fully fills at market.
    return policy_both_fill_fully(req)


def policy_partial_imbalance(yes_fill: int, no_fill: int) -> FillPolicy:
    def _f(req: OrderRequest) -> OrderResponse:
        if req.action == "sell":  # unwind of imbalance
            return policy_both_fill_fully(req)
        target = yes_fill if req.side == "yes" else no_fill
        filled = min(target, req.count)
        return OrderResponse(
            kalshi_order_id=f"kalshi-{req.client_order_id[-8:]}",
            client_order_id=req.client_order_id,
            filled_count=filled,
            requested_count=req.count,
            avg_fill_price_cents=req.limit_cents,
            fees_cents=2 * filled,
        )
    return _f


def policy_unwind_never_fills(req: OrderRequest) -> OrderResponse:
    # Buy legs fill; sell legs (unwind) stall forever -- combined with
    # executor's 5s timeout to trigger UnwindFailed.
    if req.action == "buy":
        if req.side == "yes":
            return policy_both_fill_fully(req)
        return policy_both_reject(req)
    # Sell (unwind) -- return partial to force UnwindFailed.
    return OrderResponse(
        kalshi_order_id=None,
        client_order_id=req.client_order_id,
        filled_count=0,
        requested_count=req.count,
        avg_fill_price_cents=0,
        fees_cents=0,
        error="unwind_rejected",
    )
