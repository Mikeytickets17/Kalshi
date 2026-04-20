"""Structural arb scanner (Module 2).

Input: a stream of orderbook events (deltas or snapshots) per market.
Output: Opportunity objects whenever a market's best YES ask + best NO ask
sums to less than $1 by more than (fees + slippage_buffer + min_edge_cents).

Hard rules enforced here:
  - Never emit for a HALTED market (see book.py state machine).
  - Never emit when either side's best-ask quantity is zero.
  - Every check is recorded to opportunities_detected regardless of outcome
    (traded / skipped / rejected) so the ledger is complete.

Not in scope (lives in Module 3):
  - Position sizing (Kelly, caps, bankroll math).
  - Order placement / unwinding.
  - Kill-switch enforcement.

The scanner ONLY detects and records. The sizer decides, the executor fires.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

from .. import clock, log
from .book import OrderBook
from .fees import FeeModel
from .opportunity import INTERFACE_VERSION, Opportunity

_log = log.get("scanner")


@dataclass(frozen=True)
class ScannerConfig:
    min_edge_cents: float = 1.0
    slippage_buffer_cents: float = 0.5
    min_expected_profit_cents: float = 50.0  # 0.50 USD per trade overhead floor


# Decision labels written to opportunities_detected.
DECISION_EMIT = "emit"
DECISION_SKIP_EMPTY = "skip_empty_side"
DECISION_SKIP_HALTED = "skip_halted"
DECISION_SKIP_SUM_GE_100 = "skip_sum_ge_100"
DECISION_SKIP_BELOW_EDGE = "skip_below_edge"


@dataclass
class ScanDecision:
    """Every call to scan() produces one of these, whether or not an Opportunity
    is emitted. The persist callback writes ALL decisions to the event store
    so the ledger is complete and backtests have perfect replay fidelity."""

    market_ticker: str
    ts_ms: int
    decision: str
    reason: str | None
    opportunity: Opportunity | None


class StructuralArbScanner:
    def __init__(
        self,
        config: ScannerConfig,
        fee_model: FeeModel | None = None,
        *,
        on_decision: Callable[[ScanDecision], None] | None = None,
    ) -> None:
        self.config = config
        self.fee_model = fee_model or FeeModel.builtin()
        self._on_decision = on_decision
        self.interface_version = INTERFACE_VERSION

    # ---- single-book check ----

    def scan(self, book: OrderBook, *, now_ms: int | None = None) -> ScanDecision:
        ts_ms = now_ms if now_ms is not None else clock.now_ms()

        # Halted check FIRST -- we don't even want to look at best-asks on a
        # market whose book machinery flagged it as paused or empty-too-long.
        if book.is_halted():
            return self._record(
                ScanDecision(
                    market_ticker=book.ticker,
                    ts_ms=ts_ms,
                    decision=DECISION_SKIP_HALTED,
                    reason="book state HALTED (paused or empty past threshold)",
                    opportunity=None,
                )
            )

        yes = book.best_ask("yes")
        no = book.best_ask("no")
        if yes is None or no is None:
            return self._record(
                ScanDecision(
                    market_ticker=book.ticker,
                    ts_ms=ts_ms,
                    decision=DECISION_SKIP_EMPTY,
                    reason=(
                        f"yes_ask={'present' if yes else 'none'}, "
                        f"no_ask={'present' if no else 'none'}"
                    ),
                    opportunity=None,
                )
            )

        sum_cents = yes.price_cents + no.price_cents
        if sum_cents >= 100:
            return self._record(
                ScanDecision(
                    market_ticker=book.ticker,
                    ts_ms=ts_ms,
                    decision=DECISION_SKIP_SUM_GE_100,
                    reason=f"sum_cents={sum_cents} (no structural edge)",
                    opportunity=None,
                )
            )

        fees = self.fee_model.structural_arb_fee_cents(yes.price_cents, no.price_cents)
        net_edge = 100.0 - sum_cents - fees - self.config.slippage_buffer_cents
        if net_edge < self.config.min_edge_cents:
            return self._record(
                ScanDecision(
                    market_ticker=book.ticker,
                    ts_ms=ts_ms,
                    decision=DECISION_SKIP_BELOW_EDGE,
                    reason=(
                        f"net_edge={net_edge:.2f}c "
                        f"(sum={sum_cents}c, fees={fees}c, slip="
                        f"{self.config.slippage_buffer_cents}c); "
                        f"min required {self.config.min_edge_cents}c"
                    ),
                    opportunity=None,
                )
            )

        opp = Opportunity(
            market_ticker=book.ticker,
            detected_ts_ms=ts_ms,
            yes_ask_cents=yes.price_cents,
            yes_ask_qty=yes.qty,
            no_ask_cents=no.price_cents,
            no_ask_qty=no.qty,
            sum_cents=sum_cents,
            est_fees_cents=fees,
            slippage_buffer_cents=int(self.config.slippage_buffer_cents),
            net_edge_cents=round(net_edge, 2),
            max_liquidity_contracts=min(yes.qty, no.qty),
        )
        return self._record(
            ScanDecision(
                market_ticker=book.ticker,
                ts_ms=ts_ms,
                decision=DECISION_EMIT,
                reason=None,
                opportunity=opp,
            )
        )

    # ---- multi-book batch ----

    def scan_all(self, books: Iterable[OrderBook]) -> list[ScanDecision]:
        return [self.scan(b) for b in books]

    # ---- internal ----

    def _record(self, decision: ScanDecision) -> ScanDecision:
        if self._on_decision is not None:
            try:
                self._on_decision(decision)
            except Exception as exc:  # noqa: BLE001
                _log.error("scanner.on_decision_failed", error=str(exc))
        return decision
