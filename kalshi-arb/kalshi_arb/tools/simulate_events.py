"""Insert synthetic events into the shared event store so the operator
can verify the dashboard's SSE pipeline is live without running the bot.

Use
---
    python -m kalshi_arb.tools.simulate_events           # one of each
    python -m kalshi_arb.tools.simulate_events --count 20
    python -m kalshi_arb.tools.simulate_events --rate 10 --duration 5

Points at the same SQLite file the dashboard reads from
(EVENT_STORE_PATH env var or ./data/kalshi.db default). Inserts
opportunity + execution + kill_switch rows via the normal EventStore
domain helpers, which populate change_log automatically -- so the
dashboard's ChangeCapture task picks them up within ~1 second.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import time
from pathlib import Path

from .. import log
from ..store import EventStore, SqliteBackend

_log = log.get("tools.simulate")


async def _insert_one(store: EventStore, rng: random.Random, i: int) -> None:
    kind = rng.choice(["opportunity", "opportunity", "execution", "kill_switch"])
    if kind == "opportunity":
        store.record_opportunity(
            ticker=f"KXSIM-{i:04d}",
            ts_ms=int(time.time() * 1000),
            yes_ask_cents=rng.randint(30, 60),
            yes_ask_qty=rng.randint(10, 500),
            no_ask_cents=rng.randint(30, 60),
            no_ask_qty=rng.randint(10, 500),
            sum_cents=rng.randint(85, 98),
            est_fees_cents=rng.randint(2, 6),
            slippage_buffer=0,
            net_edge_cents=round(rng.uniform(1.0, 8.0), 2),
            max_size_liquidity=rng.randint(10, 500),
            kelly_size=rng.randint(1, 100),
            hard_cap_size=rng.randint(1, 50),
            final_size=rng.randint(0, 50),
            decision=rng.choice(["emit", "skip_below_edge", "skip_empty_side"]),
        )
    elif kind == "kill_switch":
        store.record_kill_switch_change(
            tripped=rng.random() < 0.5,
            reason=rng.choice(["simulate-manual", "simulate-auto-loss-limit"]),
        )
    else:
        # execution entries: we don't have a record_execution domain helper
        # yet, so write directly into change_log via submit() for realism.
        from ..store.db import WriteJob
        import json

        store.submit(
            WriteJob(
                "INSERT INTO change_log(entity_type, entity_id, last_modified_ms, payload)"
                " VALUES(?,?,?,?)",
                (
                    "execution",
                    None,
                    int(time.time() * 1000),
                    json.dumps(
                        {
                            "outcome": rng.choice(
                                ["both_filled", "one_filled_unwound", "both_rejected"]
                            ),
                            "net_fill_cents": rng.randint(-200, 200),
                            "contracts": rng.randint(1, 50),
                            "simulated": True,
                        }
                    ),
                ),
            )
        )


async def run(count: int, rate: float, duration: float) -> None:
    from .._paths import default_event_store_path

    path = default_event_store_path()
    print(f"[simulate] event store: {path}")

    store = EventStore(SqliteBackend(path))
    await store.start()
    try:
        rng = random.Random()
        total = count if duration <= 0 else int(rate * duration)
        sleep_between = 0.0 if rate <= 0 else 1.0 / rate

        start = time.monotonic()
        for i in range(total):
            await _insert_one(store, rng, i)
            if i % 50 == 0:
                print(f"[simulate] queued {i}/{total}...")
            if sleep_between > 0:
                await asyncio.sleep(sleep_between)
        # Allow the writer coroutine to drain the queue before we exit.
        await asyncio.sleep(1.0)
        elapsed = time.monotonic() - start
        stats = store.stats()
        print(
            f"[simulate] done: {total} events in {elapsed:.1f}s, "
            f"written={stats['written_total']} dropped={stats['dropped_total']}"
        )
    finally:
        await store.stop()


def main() -> int:
    p = argparse.ArgumentParser(description="Insert synthetic events for dashboard smoke testing.")
    p.add_argument("--count", type=int, default=3, help="total events (used when --duration=0)")
    p.add_argument("--rate", type=float, default=0.0, help="events per second (paired with --duration)")
    p.add_argument("--duration", type=float, default=0.0, help="seconds to run at --rate")
    args = p.parse_args()
    asyncio.run(run(args.count, args.rate, args.duration))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
