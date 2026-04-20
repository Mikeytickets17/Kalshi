"""Kalshi fee model.

Kalshi's taker fee schedule for binary markets (published in the fee page
and on the exchange rules docs): per-contract fee equals

    fee_per_contract_dollars = ceil( 0.07 * count * price * (1 - price) * 100 ) / 100

where `price` is the fill price in dollars (0.01 .. 0.99). The fee is
rounded UP to the nearest cent per contract. The parabola peaks at
price=0.50 (where the fee is 0.07 * 0.25 = $0.0175 → rounded up to $0.02)
and hits zero at the extremes. Both legs of a structural arb are taker
crosses (IOC), so we apply taker fees on both sides.

Makers earn rebates during some promotional windows (see fees.yaml for
date-keyed schedule). The scanner ignores rebates by default -- counting
them would make the edge calculation optimistic. Override via
FeeModel(include_maker_rebate=True) only when you've verified the
rebate window is currently active.

The coefficients and any future schedule changes are kept in
config/fees.yaml rather than hardcoded so we can update without a
redeploy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


DEFAULT_FEES_PATH = Path("config/fees.yaml")


@dataclass(frozen=True)
class FeeTier:
    """One row of the fee schedule. Effective-from date controls which
    row is active on any given day."""

    effective_from: str  # ISO date, e.g. "2025-01-01"
    taker_coeff: float  # coefficient in: ceil(coeff * count * p * (1-p) * 100)/100
    maker_coeff: float = 0.0  # 0 in most tiers; occasionally negative (rebate)

    def fee_per_contract_cents(self, price_cents: int, *, side: str = "taker") -> int:
        if not (1 <= price_cents <= 99):
            raise ValueError(f"price_cents must be 1..99, got {price_cents}")
        p = price_cents / 100.0
        coeff = self.taker_coeff if side == "taker" else self.maker_coeff
        raw_dollars = coeff * 1 * p * (1.0 - p)
        # Round UP to next cent. For zero or negative coefficients just round
        # toward zero (negative = rebate).
        if raw_dollars >= 0:
            return math.ceil(raw_dollars * 100)
        return -math.ceil(-raw_dollars * 100)


# Built-in default matches Kalshi's publicly documented 7% coefficient.
BUILTIN_DEFAULT_TIER = FeeTier(
    effective_from="2024-01-01",
    taker_coeff=0.07,
    maker_coeff=0.0,
)


@dataclass
class FeeModel:
    tiers: list[FeeTier]
    include_maker_rebate: bool = False

    @classmethod
    def builtin(cls) -> "FeeModel":
        return cls(tiers=[BUILTIN_DEFAULT_TIER])

    @classmethod
    def load(cls, path: Path = DEFAULT_FEES_PATH) -> "FeeModel":
        if not path.exists():
            return cls.builtin()
        with path.open() as f:
            data = yaml.safe_load(f) or {}
        raw_tiers = data.get("tiers") or []
        tiers = []
        for row in raw_tiers:
            tiers.append(
                FeeTier(
                    effective_from=str(row["effective_from"]),
                    taker_coeff=float(row["taker_coeff"]),
                    maker_coeff=float(row.get("maker_coeff", 0.0)),
                )
            )
        if not tiers:
            tiers = [BUILTIN_DEFAULT_TIER]
        return cls(tiers=tiers)

    def active_tier(self, when: datetime | None = None) -> FeeTier:
        """Return the tier whose effective_from is the latest date <= when."""
        when = when or datetime.now(timezone.utc)
        # Sort descending by effective_from and pick the first row <= when.
        applicable = [
            t for t in self.tiers
            if _parse_iso_date(t.effective_from) <= when.replace(tzinfo=timezone.utc)
        ]
        if not applicable:
            return BUILTIN_DEFAULT_TIER
        applicable.sort(key=lambda t: t.effective_from, reverse=True)
        return applicable[0]

    # ---- used by scanner ----

    def structural_arb_fee_cents(
        self, yes_price_cents: int, no_price_cents: int, *, when: datetime | None = None
    ) -> int:
        """Total taker fee (cents) for buying 1 YES at yes_price + 1 NO at no_price.

        Both legs are taker. If include_maker_rebate=True we apply a rebate on
        whichever leg is currently posted as maker -- but in an IOC arb fire
        that's never; default stays 'both taker'.
        """
        tier = self.active_tier(when)
        return (
            tier.fee_per_contract_cents(yes_price_cents, side="taker")
            + tier.fee_per_contract_cents(no_price_cents, side="taker")
        )


def _parse_iso_date(s: str) -> datetime:
    # Accepts 'YYYY-MM-DD' or full ISO. Always returns UTC.
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        dt = datetime.strptime(s, "%Y-%m-%d")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
