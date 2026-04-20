"""Half-Kelly sizer with liquidity + hard-cap + min-profit guards.

See sizer/EXECUTOR_INTERFACE.md for the full rule list. This module is
pure: it reads no API, holds no state, consults no clock. Every input
comes from (Opportunity, BankrollSnapshot). That's what makes the unit
tests trivial and deterministic.

Rule order matters and is deliberate -- early rejects save compute
but more importantly produce the most useful 'reason' string in audit
logs. A stale bankroll is a very different story from 'kelly says 3
but min-profit floor rejects'.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..scanner.opportunity import Opportunity
from .types import INTERFACE_VERSION, BankrollSnapshot, SizingDecision


@dataclass(frozen=True)
class SizerConfig:
    hard_cap_usd: float = 200.0
    kelly_fraction: float = 0.5
    min_expected_profit_usd: float = 0.50
    daily_loss_limit_usd: float = 500.0


class HalfKellySizer:
    """Reference Sizer implementation. Pure function wrapped in a class
    only so config is bound once at construction."""

    def __init__(self, config: SizerConfig) -> None:
        self.config = config

    # ---- main entrypoint ----

    def size(self, opp: Opportunity, bankroll: BankrollSnapshot) -> SizingDecision:
        # Rule 1: stale snapshot -> refuse immediately.
        if bankroll.stale:
            return self._skip(opp, bankroll, reason="bankroll snapshot stale")

        # Rule 2: drawdown already past daily limit -> refuse (belt + suspenders;
        # caller's kill switch should have blocked us earlier).
        daily_limit_cents = int(self.config.daily_loss_limit_usd * 100)
        if bankroll.daily_realized_pnl_cents <= -daily_limit_cents:
            return self._skip(
                opp,
                bankroll,
                reason=(
                    f"daily P&L {bankroll.daily_realized_pnl_cents}c already "
                    f"past limit -{daily_limit_cents}c"
                ),
            )

        # Rule 3: liquidity cap.
        liquidity_cap = min(opp.yes_ask_qty, opp.no_ask_qty)

        # Rule 4: half-Kelly. cost_per_contract is the sum of the two asks in
        # cents; edge per contract is the scanner's net_edge_cents.
        cost_per_contract = opp.yes_ask_cents + opp.no_ask_cents
        if cost_per_contract <= 0:
            return self._skip(opp, bankroll, reason="cost_per_contract is zero")
        kelly_fraction = self.config.kelly_fraction * (opp.net_edge_cents / 100.0)
        kelly_cents = kelly_fraction * bankroll.cash_cents
        kelly_size = max(0, math.floor(kelly_cents / cost_per_contract))

        # Rule 5: hard cap (from config, dollars -> cents -> contracts).
        hard_cap_cents = int(self.config.hard_cap_usd * 100)
        hard_cap_size = max(0, math.floor(hard_cap_cents / cost_per_contract))

        # Rule 6: combined min(liquidity, kelly, hard_cap).
        final = max(0, min(liquidity_cap, kelly_size, hard_cap_size))

        if final == 0:
            return self._skip(
                opp,
                bankroll,
                reason=(
                    f"min(liq={liquidity_cap}, kelly={kelly_size}, "
                    f"hard={hard_cap_size})=0 (likely bankroll too low)"
                ),
                liquidity_cap=liquidity_cap,
                kelly_size=kelly_size,
                hard_cap_size=hard_cap_size,
            )

        # Rule 7: min-profit floor. Reject trades where expected profit
        # doesn't clear the fee+overhead threshold.
        min_profit_cents = int(self.config.min_expected_profit_usd * 100)
        expected_profit_cents = final * opp.net_edge_cents
        if expected_profit_cents < min_profit_cents:
            return self._skip(
                opp,
                bankroll,
                reason=(
                    f"expected profit {expected_profit_cents:.1f}c < "
                    f"min {min_profit_cents}c (size={final}, edge={opp.net_edge_cents}c)"
                ),
                liquidity_cap=liquidity_cap,
                kelly_size=kelly_size,
                hard_cap_size=hard_cap_size,
                min_profit_pass=False,
            )

        return SizingDecision(
            opportunity=opp,
            contracts_per_leg=final,
            reason=f"sized to {final} (min of liq/kelly/hard)",
            liquidity_cap=liquidity_cap,
            kelly_size=kelly_size,
            hard_cap_size=hard_cap_size,
            min_profit_pass=True,
            bankroll_snapshot=bankroll,
            sizer_version=INTERFACE_VERSION,
        )

    # ---- helpers ----

    def _skip(
        self,
        opp: Opportunity,
        bankroll: BankrollSnapshot,
        *,
        reason: str,
        liquidity_cap: int = 0,
        kelly_size: int = 0,
        hard_cap_size: int = 0,
        min_profit_pass: bool = False,
    ) -> SizingDecision:
        return SizingDecision(
            opportunity=opp,
            contracts_per_leg=0,
            reason=reason,
            liquidity_cap=liquidity_cap,
            kelly_size=kelly_size,
            hard_cap_size=hard_cap_size,
            min_profit_pass=min_profit_pass,
            bankroll_snapshot=bankroll,
            sizer_version=INTERFACE_VERSION,
        )
