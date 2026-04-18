"""Live Kalshi API wrapper.

Thin async adapter over pykalshi.AsyncKalshiClient. Only responsibility
is translating between our OrderRequest/OrderResponse types and pykalshi's
method signatures so nothing downstream depends on pykalshi directly.

Safety guard: refuses to construct if PAPER_MODE=true. Pair with
PaperKalshiAPI's mirror guard (refuses if LIVE_TRADING=true). Two walls
means you can't build both in the same process and can't cross-wire
by accident.
"""

from __future__ import annotations

import os
from pathlib import Path

from .. import clock, log
from .kalshi_api import OrderRequest, OrderResponse, PortfolioSnapshot

_log = log.get("executor.live")


def _env_bool(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key, "").strip().lower()
    if raw == "":
        return default
    return raw in ("1", "true", "yes", "on")


class LiveKalshiAPI:
    """Async adapter over pykalshi.AsyncKalshiClient."""

    def __init__(self, api_key_id: str, private_key_path: Path, demo: bool) -> None:
        if _env_bool("PAPER_MODE"):
            raise RuntimeError(
                "LiveKalshiAPI refused: PAPER_MODE=true in the environment. "
                "Instantiate PaperKalshiAPI instead, or unset PAPER_MODE. "
                "This guard prevents live/paper classes from coexisting in one process."
            )
        from pykalshi.aclient import AsyncKalshiClient

        self._client = AsyncKalshiClient(
            api_key_id=api_key_id,
            private_key_path=str(private_key_path),
            demo=demo,
        )
        _log.info("live_api.initialized", demo=demo)

    async def place_order(self, req: OrderRequest) -> OrderResponse:
        try:
            # pykalshi's create_order accepts keyword args mapping directly.
            kwargs = {
                "ticker": req.market_ticker,
                "action": req.action,
                "side": req.side,
                "type": req.order_type,
                "count": req.count,
                "client_order_id": req.client_order_id,
                "time_in_force": req.time_in_force,
            }
            if req.order_type == "limit":
                price_key = "yes_price" if req.side == "yes" else "no_price"
                kwargs[price_key] = req.limit_cents
            resp = await self._client.create_order(**kwargs)
        except Exception as exc:  # noqa: BLE001
            return OrderResponse(
                kalshi_order_id=None,
                client_order_id=req.client_order_id,
                filled_count=0,
                requested_count=req.count,
                avg_fill_price_cents=0,
                fees_cents=0,
                error=str(exc)[:300],
            )

        order_id = getattr(resp, "order_id", None) or (
            resp.get("order_id") if isinstance(resp, dict) else None
        )
        filled = int(getattr(resp, "filled_count", 0) or (
            resp.get("filled_count", 0) if isinstance(resp, dict) else 0
        ))
        avg_price = int(getattr(resp, "avg_fill_price_cents", 0) or (
            resp.get("avg_fill_price", 0) if isinstance(resp, dict) else 0
        ))
        fees = int(getattr(resp, "fees_cents", 0) or (
            resp.get("fees", 0) if isinstance(resp, dict) else 0
        ))
        return OrderResponse(
            kalshi_order_id=order_id,
            client_order_id=req.client_order_id,
            filled_count=filled,
            requested_count=req.count,
            avg_fill_price_cents=avg_price,
            fees_cents=fees,
            error=None,
        )

    async def cancel_order(self, kalshi_order_id: str) -> None:
        try:
            await self._client.cancel_order(order_id=kalshi_order_id)
        except Exception as exc:  # noqa: BLE001
            _log.warning("live_api.cancel_failed", order_id=kalshi_order_id, error=str(exc))

    async def get_portfolio(self) -> PortfolioSnapshot:
        try:
            bal = await self._client.portfolio.get_balance()
            positions_raw = await self._client.portfolio.get_positions()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"portfolio read failed: {exc}") from exc
        cash = int(getattr(bal, "balance_cents", 0) or (
            bal.get("balance", 0) if isinstance(bal, dict) else 0
        ))
        positions: dict[str, int] = {}
        for p in positions_raw or []:
            ticker = getattr(p, "ticker", None) or (
                p.get("ticker") if isinstance(p, dict) else None
            )
            count = getattr(p, "position", None) or (
                p.get("position") if isinstance(p, dict) else 0
            )
            if ticker is not None:
                positions[str(ticker)] = int(count or 0)
        return PortfolioSnapshot(cash_cents=cash, positions=positions, at_ms=clock.now_ms())
