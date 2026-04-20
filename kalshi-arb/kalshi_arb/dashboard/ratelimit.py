"""Per-IP in-memory token bucket for the /login endpoint.

Simple enough for a single-operator dashboard on a single machine.
Not suitable for multi-instance deployments (state is per-process).
If we ever scale to >1 dashboard machine we'd swap this for Redis
or a Fly-level rate limit. Until then, in-process is correct.

Algorithm: sliding-window counter with 1-second resolution. For each
IP we keep a list of timestamps within the past 60 seconds. Each
attempt appends its timestamp; attempts beyond the configured per-
minute limit return False.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field


WINDOW_SEC = 60.0


@dataclass
class RateLimiter:
    max_per_min: int
    _buckets: dict[str, deque[float]] = field(
        default_factory=lambda: defaultdict(deque)
    )

    def allow(self, ip: str, *, now: float | None = None) -> bool:
        now = now if now is not None else time.monotonic()
        cutoff = now - WINDOW_SEC
        bucket = self._buckets[ip]
        # Evict old entries
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= self.max_per_min:
            return False
        bucket.append(now)
        return True

    def reset(self, ip: str) -> None:
        self._buckets.pop(ip, None)
