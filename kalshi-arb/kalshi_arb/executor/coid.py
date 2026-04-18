"""Deterministic client_order_id generator.

Review note A: every order must carry an ID derived from
(market_ticker, detected_ts_ms, side, purpose). Retries / crash-restart
produce the same ID so Kalshi dedupes server-side. Non-optional for
live trading.

Hash: SHA256 truncated to 28 hex chars + prefix 'kac_'. 32 chars total.
Kalshi's public docs don't specify an upper bound on client_order_id but
well under typical UUID length (36) and well above the birthday-bound
for collisions in any realistic trading horizon.
"""

from __future__ import annotations

import hashlib


COID_PREFIX = "kac_"
COID_HASH_LEN = 28


def client_order_id(
    market_ticker: str,
    detected_ts_ms: int,
    side: str,
    purpose: str = "arb",
) -> str:
    if side not in ("yes", "no"):
        raise ValueError(f"side must be 'yes' or 'no', got {side!r}")
    if purpose not in ("arb", "unwind"):
        raise ValueError(f"purpose must be 'arb' or 'unwind', got {purpose!r}")
    raw = f"{market_ticker}|{detected_ts_ms}|{side}|{purpose}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:COID_HASH_LEN]
    return COID_PREFIX + digest
