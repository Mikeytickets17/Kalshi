"""Monotonic + wall clocks.

All event-store timestamps are epoch milliseconds, UTC. Use these helpers
everywhere to avoid mixing seconds with ms.
"""

from __future__ import annotations

import time


def now_ms() -> int:
    return int(time.time() * 1000)


def now_sec() -> float:
    return time.time()


def monotonic_ms() -> int:
    return int(time.monotonic() * 1000)
