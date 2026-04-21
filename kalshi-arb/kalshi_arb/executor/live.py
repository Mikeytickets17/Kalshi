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
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .. import clock, log
from ..probe.analysis import (
    fees_cents_from_response,
    fill_count_from_response,
    fill_price_cents_from_response,
    order_id_from_response,
)
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
            # pykalshi's real order API is client.portfolio.place_order,
            # NOT client.create_order. Args:
            #   * count_fp: fixed-point decimal STRING ("10", not 10)
            #   * yes_price_dollars / no_price_dollars: dollar STRING
            #     ("0.42", not 42). We translate from our internal
            #     integer-cent representation here.
            #   * time_in_force: pykalshi's TimeInForce enum; map "IOC"
            #     -> TimeInForce.IOC and leave other strings raw so the
            #     SDK can validate.
            from pykalshi.enums import TimeInForce

            tif_map = {
                "IOC": TimeInForce.IOC,
                "GTC": TimeInForce.GTC,
                "FOK": TimeInForce.FOK,
            }
            kwargs = {
                "ticker": req.market_ticker,
                "action": req.action,
                "side": req.side,
                "count_fp": str(req.count),
                "client_order_id": req.client_order_id,
                "time_in_force": tif_map.get(req.time_in_force, req.time_in_force),
            }
            if req.order_type == "limit":
                price_str = f"{req.limit_cents / 100:.2f}"
                if req.side == "yes":
                    kwargs["yes_price_dollars"] = price_str
                else:
                    kwargs["no_price_dollars"] = price_str
            resp = await self._client.portfolio.place_order(**kwargs)
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

        # pykalshi Order field shape:
        #   fill_count_fp          (str, fixed-point)  -> int contracts
        #   taker_fill_cost_dollars(str, '0.42')       -> int cents
        #   taker_fees_dollars     (str)               -> summed int cents
        # Helpers live in probe/analysis.py so probe + live API share
        # one canonical extractor.
        return OrderResponse(
            kalshi_order_id=order_id_from_response(resp),
            client_order_id=req.client_order_id,
            filled_count=fill_count_from_response(resp),
            requested_count=req.count,
            avg_fill_price_cents=fill_price_cents_from_response(resp),
            fees_cents=fees_cents_from_response(resp),
            error=None,
        )

    async def cancel_order(self, kalshi_order_id: str) -> None:
        try:
            await self._client.portfolio.cancel_order(order_id=kalshi_order_id)
        except Exception as exc:  # noqa: BLE001
            _log.warning("live_api.cancel_failed", order_id=kalshi_order_id, error=str(exc))

    async def get_portfolio(self) -> PortfolioSnapshot:
        try:
            bal = await self._client.portfolio.get_balance()
            positions_raw = await self._client.portfolio.get_positions()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"portfolio read failed: {exc}") from exc
        # BalanceModel.balance is already int cents (confirmed via
        # /usr/local/lib/.../pykalshi/models.py::BalanceModel). The
        # pre-audit code looked for `.balance_cents` which doesn't
        # exist -- that would have reported cash_cents=0 for every
        # live portfolio read and made the degraded-mode monitor
        # flip on the first execution.
        cash = int(getattr(bal, "balance", 0) or (
            bal.get("balance", 0) if isinstance(bal, dict) else 0
        ))
        positions: dict[str, int] = {}
        for p in positions_raw or []:
            ticker = getattr(p, "ticker", None) or (
                p.get("ticker") if isinstance(p, dict) else None
            )
            # PositionModel.position_fp is a fixed-point STRING like
            # "10" or "-5.5". Parse via Decimal -> int. Pre-audit code
            # read `.position` which doesn't exist.
            count_raw = getattr(p, "position_fp", None) or (
                p.get("position_fp") if isinstance(p, dict) else None
            )
            count = 0
            if count_raw is not None and count_raw != "":
                try:
                    count = int(Decimal(str(count_raw)))
                except (InvalidOperation, ValueError, TypeError):
                    count = 0
            if ticker is not None:
                positions[str(ticker)] = count
        return PortfolioSnapshot(cash_cents=cash, positions=positions, at_ms=clock.now_ms())
