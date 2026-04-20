"""Paper-mode KalshiAPI.

Implements the KalshiAPI Protocol but NEVER places a real order. Every
place_order() call is synthesized in-process against a fill model loaded
from config/paper_config.yaml. Reads (get_portfolio) delegate to a
real LiveKalshiAPI if one is provided -- this preserves the degraded-mode
monitor's ability to catch out-of-band account activity while the bot
is paper trading.

Safety
------
- Refuses to construct if LIVE_TRADING=true in the environment. Pair
  with LiveKalshiAPI's mirror guard (refuses if PAPER_MODE=true).
- Every synthesized OrderResponse carries client_order_id matching the
  request so the executor's idempotency logic works unchanged.
- Every paper order is recorded in self.placed_orders with a timestamp
  so tests + the event store have full audit trail.

Fill model (configurable via paper_config.yaml):
- full_fill_rate: OR clears fully at limit (IOC semantics)
- partial_fill_rate: book had some size; fill % ~ U(partial_min, partial_max)
- zero_fill_rate: nothing at limit, IOC expires with 0 fills
- Each leg sampled INDEPENDENTLY. Asymmetric fills are the whole point.

Unwind (market SELL) fills at raw_ask - unwind_slippage_cents (conservative;
real Kalshi unwinds land at best_bid which is typically 1-2c below best_ask).

Review Q2 clarification: paper-mode fill prices use the RAW book ask
at scan time (Opportunity.yes_ask_cents / no_ask_cents), NOT a slippage-
adjusted limit. The slippage buffer is only applied to the scanner's
edge calculation, never to the displayed/used limit.
"""

from __future__ import annotations

import math
import os
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .. import clock, log
from ..scanner.fees import FeeModel
from .kalshi_api import OrderRequest, OrderResponse, PortfolioSnapshot

_log = log.get("executor.paper")


DEFAULT_PAPER_CONFIG_PATH = Path("config/paper_config.yaml")


def _env_bool(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key, "").strip().lower()
    if raw == "":
        return default
    return raw in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class FillModel:
    full_fill_rate: float = 0.85
    partial_fill_rate: float = 0.10
    zero_fill_rate: float = 0.05
    partial_min_pct: float = 0.50
    partial_max_pct: float = 0.95

    def __post_init__(self) -> None:
        total = self.full_fill_rate + self.partial_fill_rate + self.zero_fill_rate
        if not math.isclose(total, 1.0, abs_tol=1e-6):
            raise ValueError(
                f"fill rates must sum to 1.0, got {total} "
                f"({self.full_fill_rate} + {self.partial_fill_rate} + {self.zero_fill_rate})"
            )
        if not (0.0 <= self.partial_min_pct <= self.partial_max_pct <= 1.0):
            raise ValueError(
                f"invalid partial bounds: [{self.partial_min_pct}, {self.partial_max_pct}]"
            )


@dataclass(frozen=True)
class PaperConfig:
    fill_model: FillModel
    unwind_slippage_cents: int
    use_builtin_fees: bool
    rng_seed: int | None

    @classmethod
    def load(cls, path: Path = DEFAULT_PAPER_CONFIG_PATH) -> "PaperConfig":
        if not path.exists():
            return cls(
                fill_model=FillModel(),
                unwind_slippage_cents=1,
                use_builtin_fees=True,
                rng_seed=None,
            )
        with path.open() as f:
            data = yaml.safe_load(f) or {}
        fm = data.get("fill_model") or {}
        return cls(
            fill_model=FillModel(
                full_fill_rate=float(fm.get("full_fill_rate", 0.85)),
                partial_fill_rate=float(fm.get("partial_fill_rate", 0.10)),
                zero_fill_rate=float(fm.get("zero_fill_rate", 0.05)),
                partial_min_pct=float(fm.get("partial_min_pct", 0.50)),
                partial_max_pct=float(fm.get("partial_max_pct", 0.95)),
            ),
            unwind_slippage_cents=int(data.get("unwind_slippage_cents", 1)),
            use_builtin_fees=bool(data.get("use_builtin_fees", True)),
            rng_seed=data.get("rng_seed"),
        )


@dataclass
class PaperKalshiAPI:
    """Synthesizes fills in-process. Never touches the real exchange for
    writes. Optionally delegates get_portfolio to a live reader."""

    config: PaperConfig = field(default_factory=PaperConfig.load)
    live_reader: Any = None  # LiveKalshiAPI or None; used only for get_portfolio

    # Runtime state
    placed_orders: list[OrderRequest] = field(default_factory=list)
    _rng: random.Random = field(init=False)
    _dedupe_cache: dict[str, OrderResponse] = field(default_factory=dict)
    _fee_model: FeeModel = field(init=False)
    _last_ask_by_ticker_side: dict[tuple[str, str], int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if _env_bool("LIVE_TRADING"):
            raise RuntimeError(
                "PaperKalshiAPI refused: LIVE_TRADING=true in the environment. "
                "Use LiveKalshiAPI, or unset LIVE_TRADING. This guard prevents "
                "live/paper classes from coexisting in one process."
            )
        self._rng = random.Random(self.config.rng_seed)
        self._fee_model = FeeModel.builtin() if self.config.use_builtin_fees else None
        _log.info(
            "paper_api.initialized",
            full_fill_rate=self.config.fill_model.full_fill_rate,
            partial_fill_rate=self.config.fill_model.partial_fill_rate,
            zero_fill_rate=self.config.fill_model.zero_fill_rate,
            seeded=self.config.rng_seed is not None,
        )

    # ---- write path (synthetic) ----

    async def place_order(self, req: OrderRequest) -> OrderResponse:
        """Synthesize a fill per the configured fill model. Kalshi-style
        idempotency: duplicate client_order_id returns the cached response.

        The fill model applies to BUY (limit IOC) orders only. SELL (market
        unwind) orders always fully fill at ask - unwind_slippage_cents.
        That matches Kalshi reality on liquid BTC/ETH markets: market sells
        clear the book and hit best_bid immediately. If we ever want to
        simulate unwind failure, use FakeKalshiAPIWithHangingUnwind --
        paper mode's job is to faithfully model the happy path of a normal
        Kalshi session."""
        if req.client_order_id in self._dedupe_cache:
            return self._dedupe_cache[req.client_order_id]

        # Cache the most-recent ask per (ticker, side) so unwind market
        # orders have a reference price. Buy orders set this; sell orders
        # consume it.
        if req.action == "buy" and req.limit_cents > 0:
            self._last_ask_by_ticker_side[(req.market_ticker, req.side)] = req.limit_cents

        if req.action == "sell":
            # Market unwind: full fill at ask - slippage.
            filled_count = req.count
        else:
            filled_count = self._sample_fill_count(req.count)
        if filled_count == 0:
            resp = OrderResponse(
                kalshi_order_id=None,
                client_order_id=req.client_order_id,
                filled_count=0,
                requested_count=req.count,
                avg_fill_price_cents=0,
                fees_cents=0,
                error="ioc_no_fill",
            )
        else:
            fill_price = self._fill_price(req)
            fees = self._estimate_fees(fill_price, filled_count)
            resp = OrderResponse(
                kalshi_order_id=f"paper-{req.client_order_id[-8:]}",
                client_order_id=req.client_order_id,
                filled_count=filled_count,
                requested_count=req.count,
                avg_fill_price_cents=fill_price,
                fees_cents=fees,
                error=None,
            )

        self.placed_orders.append(req)
        self._dedupe_cache[req.client_order_id] = resp
        _log.info(
            "paper_api.fill_synthesized",
            ticker=req.market_ticker,
            side=req.side,
            action=req.action,
            requested=req.count,
            filled=resp.filled_count,
            price=resp.avg_fill_price_cents,
            fees=resp.fees_cents,
            paper=True,
        )
        return resp

    async def cancel_order(self, kalshi_order_id: str) -> None:
        # No-op: paper orders never actually existed on the exchange.
        _log.debug("paper_api.cancel_noop", order_id=kalshi_order_id)

    async def get_portfolio(self) -> PortfolioSnapshot:
        """Paper mode delegates the portfolio read to the real API when
        provided. This keeps the degraded-mode monitor meaningful (it
        detects out-of-band account activity even while the bot is
        paper-trading). If no live reader is wired, returns empty."""
        if self.live_reader is not None:
            return await self.live_reader.get_portfolio()
        return PortfolioSnapshot(cash_cents=0, positions={}, at_ms=clock.now_ms())

    # ---- fill-model internals ----

    def _sample_fill_count(self, requested: int) -> int:
        """Independent per-leg sample. Review Q3 spec compliance."""
        fm = self.config.fill_model
        roll = self._rng.random()
        if roll < fm.full_fill_rate:
            return requested
        if roll < fm.full_fill_rate + fm.partial_fill_rate:
            frac = self._rng.uniform(fm.partial_min_pct, fm.partial_max_pct)
            filled = max(1, int(requested * frac))  # clamp to >= 1 for partials
            return min(filled, requested)
        return 0

    def _fill_price(self, req: OrderRequest) -> int:
        """Review clarification: BUY legs fill at the raw book ask (which
        is the IOC limit cents the scanner placed). SELL legs (unwind)
        fill at ask - unwind_slippage_cents, simulating best_bid."""
        if req.action == "buy":
            return max(1, req.limit_cents)  # Kalshi prices bounded 1..99
        # Unwind: look up the raw ask we captured from the prior buy, fall
        # back to 50c if we have no record (shouldn't happen in realistic flow).
        raw_ask = self._last_ask_by_ticker_side.get(
            (req.market_ticker, req.side), 50
        )
        fill_price = raw_ask - self.config.unwind_slippage_cents
        return max(1, min(99, fill_price))

    def _estimate_fees(self, price_cents: int, count: int) -> int:
        """Per-contract fee × count using the scanner's fee model. Matches
        live P&L accounting so paper P&L is a fair comparison."""
        if self._fee_model is None:
            return 0
        per_contract = self._fee_model.active_tier().fee_per_contract_cents(
            price_cents, side="taker"
        )
        return per_contract * count
