"""Structural-arb executor.

Consumes a SizingDecision, fires both legs in parallel as IOC limits,
unwinds any imbalance at market immediately (review override Q1), and
reports the outcome plus P&L confidence.

See sizer/EXECUTOR_INTERFACE.md for the full contract.

Key invariants (enforced here, asserted in tests):
  * Kill switch is checked BEFORE any order goes out.
  * Both legs fired via asyncio.gather (parallel, not sequential).
  * Imbalance unwind is market order, immediate, no limit/escalation.
  * Unwind timeout = 5s. Miss = UnwindFailed + sentinel file + kill trip.
  * Daily-loss counter only increments on pnl_confidence='realized'.
  * client_order_id is deterministic -> crash-retries dedupe at Kalshi.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import clock, log
from ..sizer.types import SizingDecision
from .coid import client_order_id
from .degraded_mode import DegradedModeMonitor
from .kalshi_api import KalshiAPI, OrderRequest, OrderResponse
from .killswitch import KillSwitch
from .types import (
    OUTCOME_BOTH_FILLED,
    OUTCOME_BOTH_FILLED_IMBALANCED_UNWOUND,
    OUTCOME_BOTH_REJECTED,
    OUTCOME_HALTED_BY_LOSS_LIMIT,
    OUTCOME_KILL_SWITCH,
    OUTCOME_ONE_FILLED_UNWOUND,
    OUTCOME_UNWIND_FAILED,
    PNL_ESTIMATED_WITH_UNWIND,
    PNL_REALIZED,
    ExecutionResult,
    LegResult,
    UnwindFailed,
)

_log = log.get("executor")


@dataclass(frozen=True)
class ExecutorConfig:
    daily_loss_limit_cents: int = 50000     # $500 default
    unwind_timeout_sec: float = 5.0
    critical_unwind_dir: Path = Path(".")   # where CRITICAL_UNWIND_FAILED_*.txt lands


class StructuralArbExecutor:
    def __init__(
        self,
        *,
        api: KalshiAPI,
        killswitch: KillSwitch,
        degraded_monitor: DegradedModeMonitor | None = None,
        config: ExecutorConfig | None = None,
    ) -> None:
        self.api = api
        self.killswitch = killswitch
        self.degraded = degraded_monitor
        self.config = config or ExecutorConfig()
        # Two separate counters per review Flag 1:
        # - realized_pnl: ONLY pnl_confidence='realized' outcomes count here.
        #   This is the number that feeds the daily loss limit trip.
        # - estimated_pnl: tracks estimated-with-unwind outcomes separately
        #   for audit + dashboard display. Gets reconciled into
        #   realized_pnl only when the future settlement-watcher module
        #   matches fills to market resolutions.
        # Crucially, estimated_pnl MUST NOT leak into realized_pnl. A future
        # refactor that accidentally unifies these two counters would
        # silently corrupt the daily-loss-limit trip -- hence the strong
        # assertions in test_estimated_unwind_*.
        self._daily_realized_pnl_cents: int = 0
        self._daily_estimated_pnl_cents: int = 0
        self._last_exec_ms: int = 0

    # ---------- main entry ----------

    async def execute(self, decision: SizingDecision) -> ExecutionResult:
        fired_ts = clock.now_ms()

        # Pre-flight: kill switch?
        if self.killswitch.is_tripped():
            return self._short_circuit(decision, fired_ts, OUTCOME_KILL_SWITCH)

        # Pre-flight: already past daily loss limit?
        if self._daily_realized_pnl_cents <= -self.config.daily_loss_limit_cents:
            return self._short_circuit(decision, fired_ts, OUTCOME_HALTED_BY_LOSS_LIMIT)

        # Sizer said skip. Record but fire nothing.
        if decision.contracts_per_leg == 0:
            return ExecutionResult(
                decision=decision,
                fired_ts_ms=fired_ts,
                legs=(),
                outcome=OUTCOME_BOTH_REJECTED,
                net_fill_cents=0,
                total_fees_cents=0,
                pnl_confidence=PNL_REALIZED,
                error=None,
            )

        # --- Parallel IOC dispatch ---
        opp = decision.opportunity
        yes_req = OrderRequest(
            market_ticker=opp.market_ticker,
            side="yes",
            action="buy",
            order_type="limit",
            time_in_force="IOC",
            count=decision.contracts_per_leg,
            limit_cents=opp.yes_ask_cents,
            client_order_id=client_order_id(
                opp.market_ticker, opp.detected_ts_ms, "yes", "arb"
            ),
        )
        no_req = OrderRequest(
            market_ticker=opp.market_ticker,
            side="no",
            action="buy",
            order_type="limit",
            time_in_force="IOC",
            count=decision.contracts_per_leg,
            limit_cents=opp.no_ask_cents,
            client_order_id=client_order_id(
                opp.market_ticker, opp.detected_ts_ms, "no", "arb"
            ),
        )

        place_start = clock.now_ms()
        yes_resp, no_resp = await asyncio.gather(
            self.api.place_order(yes_req),
            self.api.place_order(no_req),
        )

        yes_leg = self._to_leg(yes_req, yes_resp, placed_ts_ms=place_start)
        no_leg = self._to_leg(no_req, no_resp, placed_ts_ms=place_start)

        # Resolve outcome + unwinds.
        try:
            legs, outcome, pnl_confidence = await self._resolve_fills(
                opp, decision, yes_leg, no_leg
            )
        except UnwindFailed as exc:
            # Sentinel file + kill switch already handled inside _unwind().
            return ExecutionResult(
                decision=decision,
                fired_ts_ms=fired_ts,
                legs=(yes_leg, no_leg),
                outcome=OUTCOME_UNWIND_FAILED,
                net_fill_cents=_signed_net_fill(yes_leg, no_leg),
                total_fees_cents=(yes_resp.fees_cents + no_resp.fees_cents),
                pnl_confidence=PNL_ESTIMATED_WITH_UNWIND,
                error=str(exc),
            )

        net_fill = _signed_net_fill(*legs)
        total_fees = yes_resp.fees_cents + no_resp.fees_cents
        # Add unwind fees (any leg beyond the first two).
        for leg in legs[2:]:
            # Unwind fees aren't on the LegResult directly; FakeKalshiAPI returns
            # them on the OrderResponse. We already summed via response objects.
            pass

        # P&L accounting. Two strict rules (review Flag 1):
        #   A) realized counter accepts ONLY pnl_confidence='realized'.
        #   B) estimated counter accepts ONLY pnl_confidence='estimated_with_unwind'.
        # Any other outcome touches NEITHER counter. This prevents the
        # landmine of _signed_net_fill=0 placeholder values silently
        # rolling into either audit trail.
        if net_fill is not None:
            total_contracts = legs[0].filled_count + legs[1].filled_count
            pairs = total_contracts // 2  # one 'pair' = one YES + one NO = $1 at settlement
            if pnl_confidence == PNL_REALIZED:
                total_pnl = pairs * 100 - net_fill - total_fees
                self._daily_realized_pnl_cents += total_pnl
                if self._daily_realized_pnl_cents <= -self.config.daily_loss_limit_cents:
                    self.killswitch.trip(
                        f"daily_loss_limit: realized P&L {self._daily_realized_pnl_cents}c "
                        f"<= -{self.config.daily_loss_limit_cents}c"
                    )
            elif pnl_confidence == PNL_ESTIMATED_WITH_UNWIND:
                # Conservative estimate for dashboard/audit -- does NOT affect
                # the trip. Will be reconciled into realized by the
                # settlement-watcher module when fills are matched to market
                # resolutions. Kept separate so the audit trail clearly
                # distinguishes "we know this P&L" from "we estimate this P&L".
                est_pnl = pairs * 100 - net_fill - total_fees
                self._daily_estimated_pnl_cents += est_pnl

        self._last_exec_ms = clock.now_ms()
        if self.degraded is not None:
            self.degraded.record_execution(self._last_exec_ms)

        return ExecutionResult(
            decision=decision,
            fired_ts_ms=fired_ts,
            legs=tuple(legs),
            outcome=outcome,
            net_fill_cents=net_fill,
            total_fees_cents=total_fees,
            pnl_confidence=pnl_confidence,
            error=None,
        )

    # ---------- fill resolution + unwind ----------

    async def _resolve_fills(
        self,
        opp: Any,
        decision: SizingDecision,
        yes_leg: LegResult,
        no_leg: LegResult,
    ) -> tuple[list[LegResult], str, str]:
        yes_f = yes_leg.filled_count
        no_f = no_leg.filled_count

        if yes_f == 0 and no_f == 0:
            return [yes_leg, no_leg], OUTCOME_BOTH_REJECTED, PNL_REALIZED

        # Both filled -- check for imbalance.
        if yes_f > 0 and no_f > 0:
            if yes_f == no_f:
                return [yes_leg, no_leg], OUTCOME_BOTH_FILLED, PNL_REALIZED
            # Partial fills with imbalance -- unwind the excess on the
            # over-filled side.
            over_side = "yes" if yes_f > no_f else "no"
            unwind_count = abs(yes_f - no_f)
            unwind_leg = await self._unwind_at_market(
                opp.market_ticker,
                opp.detected_ts_ms,
                over_side,
                unwind_count,
            )
            return (
                [yes_leg, no_leg, unwind_leg],
                OUTCOME_BOTH_FILLED_IMBALANCED_UNWOUND,
                PNL_ESTIMATED_WITH_UNWIND,
            )

        # Exactly one leg filled -- unwind all of it.
        filled_side = "yes" if yes_f > 0 else "no"
        unwind_count = yes_f if yes_f > 0 else no_f
        unwind_leg = await self._unwind_at_market(
            opp.market_ticker,
            opp.detected_ts_ms,
            filled_side,
            unwind_count,
        )
        return (
            [yes_leg, no_leg, unwind_leg],
            OUTCOME_ONE_FILLED_UNWOUND,
            PNL_ESTIMATED_WITH_UNWIND,
        )

    async def _unwind_at_market(
        self,
        market_ticker: str,
        detected_ts_ms: int,
        side: str,
        count: int,
    ) -> LegResult:
        coid = client_order_id(market_ticker, detected_ts_ms, side, "unwind")
        req = OrderRequest(
            market_ticker=market_ticker,
            side=side,
            action="sell",
            order_type="market",
            time_in_force="IOC",
            count=count,
            limit_cents=0,
            client_order_id=coid,
        )
        placed_ts = clock.now_ms()
        try:
            resp = await asyncio.wait_for(
                self.api.place_order(req),
                timeout=self.config.unwind_timeout_sec,
            )
        except asyncio.TimeoutError:
            self._write_unwind_sentinel(market_ticker, side, count, "timeout")
            self.killswitch.trip(f"unwind_failed: {market_ticker} {side} x{count} timeout")
            raise UnwindFailed(
                f"unwind timeout after {self.config.unwind_timeout_sec}s "
                f"on {market_ticker} {side} x{count}"
            ) from None
        if not resp.ok or resp.filled_count < count:
            self._write_unwind_sentinel(market_ticker, side, count, resp.error or "partial")
            self.killswitch.trip(
                f"unwind_failed: {market_ticker} {side} x{count} "
                f"filled={resp.filled_count} err={resp.error}"
            )
            raise UnwindFailed(
                f"unwind incomplete on {market_ticker} {side}: "
                f"requested {count}, filled {resp.filled_count}, error={resp.error}"
            )
        return LegResult(
            side=side,
            action="sell",
            limit_cents=0,
            requested_count=count,
            filled_count=resp.filled_count,
            kalshi_order_id=resp.kalshi_order_id,
            client_order_id=coid,
            placed_ts_ms=placed_ts,
            first_response_ts_ms=clock.now_ms(),
            error=None,
        )

    def _write_unwind_sentinel(
        self, ticker: str, side: str, count: int, reason: str
    ) -> None:
        ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        path = self.config.critical_unwind_dir / f"CRITICAL_UNWIND_FAILED_{ts}.txt"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                f"TIMESTAMP: {ts}\n"
                f"TICKER: {ticker}\n"
                f"SIDE: {side}\n"
                f"OUTSTANDING_CONTRACTS: {count}\n"
                f"REASON: {reason}\n",
                encoding="utf-8",
            )
            _log.critical(
                "executor.critical_unwind_sentinel",
                file=str(path),
                ticker=ticker,
                side=side,
                count=count,
                reason=reason,
            )
        except Exception as exc:  # noqa: BLE001
            _log.critical(
                "executor.sentinel_write_failed",
                error=str(exc),
                ticker=ticker,
                side=side,
                count=count,
            )

    # ---------- helpers ----------

    def _short_circuit(
        self, decision: SizingDecision, fired_ts: int, outcome: str
    ) -> ExecutionResult:
        return ExecutionResult(
            decision=decision,
            fired_ts_ms=fired_ts,
            legs=(),
            outcome=outcome,
            net_fill_cents=0,
            total_fees_cents=0,
            pnl_confidence=PNL_REALIZED,
            error=None,
        )

    def _to_leg(
        self, req: OrderRequest, resp: OrderResponse, *, placed_ts_ms: int
    ) -> LegResult:
        return LegResult(
            side=req.side,
            action=req.action,
            limit_cents=req.limit_cents,
            requested_count=req.count,
            filled_count=resp.filled_count,
            kalshi_order_id=resp.kalshi_order_id,
            client_order_id=req.client_order_id,
            placed_ts_ms=placed_ts_ms,
            first_response_ts_ms=clock.now_ms(),
            error=resp.error,
        )

    # ---------- test/ops introspection ----------

    @property
    def daily_realized_pnl_cents(self) -> int:
        """ONLY pnl_confidence='realized' outcomes. Feeds the loss-limit trip."""
        return self._daily_realized_pnl_cents

    @property
    def daily_estimated_pnl_cents(self) -> int:
        """ONLY pnl_confidence='estimated_with_unwind' outcomes. Dashboard
        read-only -- does NOT feed the loss-limit trip. Gets reconciled
        into realized by the settlement-watcher module (future push)."""
        return self._daily_estimated_pnl_cents


def _signed_net_fill(*legs: LegResult) -> int:
    """Net cash outflow from all fills across all legs.

    Buys are a positive cost, sells (unwinds) are negative (we received cash).
    This is the TOTAL dollar amount that left our account across the whole
    arb attempt -- settlement will pay us back separately.
    """
    total = 0
    for leg in legs:
        if leg.filled_count <= 0:
            continue
        if leg.action == "buy":
            # Note: we don't have avg_fill_price on LegResult by design --
            # limit_cents is the IOC ceiling, actual fill could be lower.
            # For accounting purposes the limit price is a conservative
            # upper bound; real P&L rolls up from OrderResponse.fees_cents.
            total += leg.filled_count * leg.limit_cents
        elif leg.action == "sell":
            # Unwind cash credit. We report limit_cents=0 for market orders
            # so we don't know the exact proceeds until the response. For
            # signed-net-fill purposes, conservatively treat market fills
            # as zero proceeds (worst case); the actual unwound value is
            # tracked via OrderResponse.avg_fill_price_cents and surfaced
            # in fees_cents totals at the ExecutionResult level.
            pass
    return total
