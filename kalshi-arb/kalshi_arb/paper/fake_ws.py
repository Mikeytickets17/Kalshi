"""In-process fake WS source for `--smoke-test` runs and unit tests.

Replaces `ShardedWS` when the CLI is invoked with `--smoke-test <N>`.
Emits deterministic orderbook_delta messages at a configurable rate
so the scanner sees realistic arb opportunities and the full pipeline
(scanner -> sizer -> executor -> store) executes without touching
Kalshi.

Design choices:
  * Deterministic. Same seed -> same deltas. Tests assert exact counts.
  * Pushes directly into a caller-supplied async handler. No queue, no
    internal task farm -- the runner owns the loop.
  * Stops cleanly when a stop Event is set or when the script count
    is exhausted.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Awaitable, Callable

from .. import log

_log = log.get("paper.fake_ws")


@dataclass(frozen=True)
class SyntheticDelta:
    ticker: str
    side: str          # 'yes' | 'no'
    price_cents: int   # 1..99
    delta: int         # contracts added (+) / removed (-)


# Each prime delta seeds YES then NO at an arb-viable spread:
# sum=90c, taker fees ~4c (2c per leg at the parabola peak nearby),
# slippage buffer 0.5c -> net_edge ~5.5c, well above the default
# 1.0c min_edge threshold. Subsequent jitter deltas keep the book
# populated without disturbing the sum.
_DEFAULT_UNIVERSE = [
    ("KXBTC-SMOKE-1", 38, 52),
    ("KXETH-SMOKE-1", 42, 48),
    ("KXRAIN-SMOKE-1", 40, 50),
]


DeltaHandler = Callable[[SyntheticDelta], Awaitable[None]]


@dataclass
class FakeWSSource:
    """Pushes deltas into a handler at `rate_per_sec` hz per ticker.

    On first pass through a ticker, emits two deltas (YES then NO) that
    populate the book with arbitrageable prices. Subsequent ticks emit
    a small delta (+/- 1) on alternating sides so the book stays fresh
    but the sum stays below 100c, producing a continuous stream of
    scanner emit decisions."""

    handler: DeltaHandler
    universe: list[tuple[str, int, int]] = None  # (ticker, yes_cents, no_cents)
    rate_per_sec: float = 5.0
    seed: int = 42

    def __post_init__(self) -> None:
        if self.universe is None:
            self.universe = list(_DEFAULT_UNIVERSE)
        self._rng = random.Random(self.seed)
        self._primed: set[str] = set()
        self._stop = asyncio.Event()

    def tickers(self) -> list[str]:
        return [t for t, _y, _n in self.universe]

    def stop(self) -> None:
        """Idempotent stop. Safe from any coroutine / signal handler."""
        self._stop.set()

    async def run(self) -> int:
        """Drive deltas until stop(). Returns the number of deltas sent."""
        sent = 0
        if self.rate_per_sec <= 0:
            return sent
        sleep_s = 1.0 / self.rate_per_sec
        _log.info(
            "fake_ws.started",
            tickers=len(self.universe),
            rate_per_sec=self.rate_per_sec,
        )
        while not self._stop.is_set():
            for ticker, yes_c, no_c in self.universe:
                if self._stop.is_set():
                    break
                if ticker not in self._primed:
                    # Prime both sides so scanner sees a populated book.
                    await self.handler(
                        SyntheticDelta(ticker, "yes", yes_c, 100)
                    )
                    await self.handler(
                        SyntheticDelta(ticker, "no", no_c, 100)
                    )
                    self._primed.add(ticker)
                    sent += 2
                else:
                    # Jitter: +/- 1 contract on a random side at the
                    # same price. Keeps books non-static but arb-viable.
                    side = self._rng.choice(("yes", "no"))
                    price = yes_c if side == "yes" else no_c
                    delta = self._rng.choice((+1, -1))
                    await self.handler(
                        SyntheticDelta(ticker, side, price, delta)
                    )
                    sent += 1
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=sleep_s
                    )
                    # _stop was set -- break out immediately.
                    break
                except asyncio.TimeoutError:
                    pass
        _log.info("fake_ws.stopped", sent=sent)
        return sent
