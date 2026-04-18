"""In-memory order book per market with halted-state machine.

The book is a flat 99-slot int array per side (prices are always integer
cents 1..99 on Kalshi). That gives O(1) delta application and a fixed
memory footprint regardless of activity level.

Halted state
------------
A market is considered halted when trading is paused or the book is
legitimately empty on both sides. The scanner must NOT treat an empty
book as a structural arb opportunity -- both-sides-empty means nobody is
quoting, which is NOT the same as YES+NO summing to <$1.

State transitions (per market):
  ACTIVE ---empty both sides for >= EMPTY_HALT_SEC---> HALTED
  HALTED --non-empty both sides for >= RECOVER_SEC---> ACTIVE
  ACTIVE --explicit status=paused-----------------> HALTED
  HALTED --explicit status=open + above recover---> ACTIVE

EMPTY_HALT_SEC / RECOVER_SEC are config-driven (see config.Config).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


NUM_PRICES = 99  # Kalshi prices are 1..99 cents; index 0 is unused
EMPTY_HALT_SEC_DEFAULT = 2.0
RECOVER_SEC_DEFAULT = 5.0


class BookState(Enum):
    ACTIVE = "active"
    HALTED = "halted"


@dataclass
class BestLevel:
    price_cents: int
    qty: int


@dataclass
class OrderBook:
    """Flat-array order book for one market.

    `yes_levels[p]` = total YES contracts resting at price p cents.
    Same for `no_levels`. Price 0 is unused (index convenience).
    """

    ticker: str
    yes_levels: list[int] = field(default_factory=lambda: [0] * (NUM_PRICES + 1))
    no_levels: list[int] = field(default_factory=lambda: [0] * (NUM_PRICES + 1))
    last_seq: int = 0
    last_update_monotonic: float = 0.0
    state: BookState = BookState.ACTIVE
    _empty_since: float | None = None
    _healthy_since: float | None = None
    empty_halt_sec: float = EMPTY_HALT_SEC_DEFAULT
    recover_sec: float = RECOVER_SEC_DEFAULT

    def apply_delta(self, side: str, price_cents: int, delta: int, *, seq: int = 0, now: float | None = None) -> None:
        if side not in ("yes", "no"):
            raise ValueError(f"side must be 'yes' or 'no', got {side!r}")
        if price_cents < 1 or price_cents > NUM_PRICES:
            raise ValueError(f"price_cents must be 1..{NUM_PRICES}, got {price_cents}")
        levels = self.yes_levels if side == "yes" else self.no_levels
        new_qty = levels[price_cents] + delta
        if new_qty < 0:
            # Defensive: Kalshi shouldn't send this, but clamp rather than crash
            new_qty = 0
        levels[price_cents] = new_qty
        self.last_seq = seq
        self.last_update_monotonic = now if now is not None else time.monotonic()
        self._reconsider_state(self.last_update_monotonic)

    def apply_snapshot(
        self,
        yes_levels: dict[int, int] | list[tuple[int, int]],
        no_levels: dict[int, int] | list[tuple[int, int]],
        *,
        seq: int = 0,
        now: float | None = None,
    ) -> None:
        self.yes_levels = [0] * (NUM_PRICES + 1)
        self.no_levels = [0] * (NUM_PRICES + 1)
        for p, q in _iter_pairs(yes_levels):
            if 1 <= p <= NUM_PRICES:
                self.yes_levels[p] = q
        for p, q in _iter_pairs(no_levels):
            if 1 <= p <= NUM_PRICES:
                self.no_levels[p] = q
        self.last_seq = seq
        self.last_update_monotonic = now if now is not None else time.monotonic()
        self._reconsider_state(self.last_update_monotonic)

    def mark_paused(self, now: float | None = None) -> None:
        self.state = BookState.HALTED
        self._healthy_since = None
        self._empty_since = now if now is not None else time.monotonic()

    # ---- queries ----

    def best_ask(self, side: str) -> BestLevel | None:
        """Lowest price with positive quantity on the given side. None if empty."""
        levels = self.yes_levels if side == "yes" else self.no_levels
        for p in range(1, NUM_PRICES + 1):
            if levels[p] > 0:
                return BestLevel(price_cents=p, qty=levels[p])
        return None

    def is_both_sides_populated(self) -> bool:
        return self.best_ask("yes") is not None and self.best_ask("no") is not None

    def is_halted(self, now: float | None = None) -> bool:
        """Re-evaluates state before answering -- handles time-based transitions
        even if no new delta has arrived (e.g. the book went empty and stayed
        empty past the halt threshold)."""
        self._reconsider_state(now if now is not None else time.monotonic())
        return self.state is BookState.HALTED

    # ---- internal ----

    def _reconsider_state(self, now: float) -> None:
        """State machine:

        HALTED is reserved for two cases:
          (a) explicit pause via mark_paused()
          (b) BOTH sides have been empty for >= empty_halt_sec

        A one-sided book (YES populated, NO empty or vice versa) is NOT
        halted -- the scanner reports SKIP_EMPTY for that case which is
        semantically distinct. 'Halted' means the market is down; 'empty
        side' means it's up but not quoted on one direction.
        """
        has_yes = self.best_ask("yes") is not None
        has_no = self.best_ask("no") is not None
        both_empty = (not has_yes) and (not has_no)
        both_populated = has_yes and has_no

        if both_empty:
            if self._empty_since is None:
                self._empty_since = now
            self._healthy_since = None
            if (now - self._empty_since) >= self.empty_halt_sec:
                self.state = BookState.HALTED
            return

        # Reset empty timer whenever at least one side is quoted.
        self._empty_since = None

        if both_populated:
            if self.state is BookState.HALTED:
                if self._healthy_since is None:
                    self._healthy_since = now
                if (now - self._healthy_since) >= self.recover_sec:
                    self.state = BookState.ACTIVE
            else:
                self._healthy_since = now
        else:
            # Exactly one side populated. Don't flip to HALTED; scanner will
            # handle via SKIP_EMPTY. Stay in whatever state we were in.
            self._healthy_since = None


def _iter_pairs(
    src: dict[int, int] | list[tuple[int, int]] | list[list[int]],
):
    if isinstance(src, dict):
        yield from src.items()
        return
    for item in src:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            yield int(item[0]), int(item[1])
