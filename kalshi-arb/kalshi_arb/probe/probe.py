"""One-shot diagnostic probe.

Measures four unknowns and writes them to config/detected_limits.yaml so the
rest of the system sizes itself against real numbers instead of guesses.

Probes
------
1. WS subscription cap — how many tickers can ONE WebSocket connection
   subscribe to via orderbook_delta before Kalshi rejects or silently drops.
2. REST write latency distribution — p50/p95/p99 round-trip for order placement
   (uses demo mode at prices that never fill so we don't leak capital).
3. REST rate-limit ceiling — ramp request rate on GET /exchange/status until
   429, record the ceiling and Retry-After.
4. End-to-end arb loop latency — WS book update → detection → demo order
   fire → fill confirmation.

Run
---
    python -m kalshi_arb.probe.probe

Results land at config/detected_limits.yaml. If AUTO_PUBLISH=true the script
commits the yaml back to a dedicated branch so remote reviewers can read it
without shell access.
"""

from __future__ import annotations

import asyncio
import statistics
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .. import clock, log
from ..config import CATEGORY_PREFIXES, Config
from ..rest.client import RestClient, RestConfig

_log = log.get("probe")

RESULTS_PATH = Path("config/detected_limits.yaml")


@dataclass
class ProbeResults:
    ts_utc: str = ""
    demo_mode: bool = True
    ws_subscription: dict[str, Any] = field(default_factory=dict)
    rest_write_latency_ms: dict[str, Any] = field(default_factory=dict)
    rest_rate_limit: dict[str, Any] = field(default_factory=dict)
    end_to_end_loop_ms: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


async def probe_ws_subscription_cap(
    rest: RestClient, ceiling_tickers: list[str]
) -> dict[str, Any]:
    """Subscribe to orderbook_delta on an expanding slice of tickers.

    We grow the slice in steps of 50 and watch for: (a) Kalshi-side error
    responses, (b) silent drop-offs where message flow stops for >10s after
    the subscribe acknowledgement.
    """
    _log.info("probe.ws.start", ceiling=len(ceiling_tickers))
    result: dict[str, Any] = {
        "max_confirmed_tickers": 0,
        "failed_at_tickers": None,
        "failure_mode": None,
        "steps": [],
    }
    step_size = 50
    feed = rest.underlying.feed()
    msg_counter: dict[str, int] = {}

    async with feed as f:
        @f.on("orderbook_delta")
        def _count(msg: Any) -> None:
            t = getattr(msg, "market_ticker", None) or (
                msg.get("market_ticker") if isinstance(msg, dict) else None
            )
            if t:
                msg_counter[t] = msg_counter.get(t, 0) + 1

        for upper in range(step_size, len(ceiling_tickers) + 1, step_size):
            slice_ = ceiling_tickers[:upper]
            try:
                f.subscribe("orderbook_delta", market_tickers=slice_)
            except Exception as exc:  # noqa: BLE001
                result["failed_at_tickers"] = upper
                result["failure_mode"] = f"subscribe_error: {exc}"
                _log.warning("probe.ws.subscribe_rejected", at=upper, error=str(exc))
                break

            # Wait for at least one message from ≥25% of slice or 10s timeout.
            before = sum(1 for t in slice_ if msg_counter.get(t))
            wait_start = time.monotonic()
            while time.monotonic() - wait_start < 10.0:
                active = sum(1 for t in slice_ if msg_counter.get(t))
                if active - before >= max(1, len(slice_) // 4):
                    break
                await asyncio.sleep(0.5)

            active = sum(1 for t in slice_ if msg_counter.get(t))
            result["steps"].append(
                {
                    "subscribed": upper,
                    "receiving_messages": active,
                    "coverage_pct": round(100 * active / max(1, upper), 1),
                }
            )
            _log.info("probe.ws.step", subscribed=upper, active=active)
            if active < upper * 0.25 and upper > step_size:
                result["failed_at_tickers"] = upper
                result["failure_mode"] = f"silent_drop_off at {active}/{upper}"
                break
            result["max_confirmed_tickers"] = upper

    return result


async def probe_rest_write_latency(rest: RestClient, ticker: str, samples: int = 100) -> dict[str, Any]:
    """Fire non-filling demo orders and measure placement round-trip.

    Uses a price of 1¢ on YES so no marketable cross happens. Immediately
    cancels every order after placement.
    """
    _log.info("probe.rest_write.start", samples=samples, ticker=ticker)
    latencies_ms: list[float] = []
    errors = 0

    for i in range(samples):
        t0 = time.monotonic()
        order_id = None
        try:
            resp = await asyncio.to_thread(
                rest.underlying.create_order,
                ticker=ticker,
                action="buy",
                side="yes",
                type="limit",
                count=1,
                yes_price=1,
            )
            latency_ms = (time.monotonic() - t0) * 1000
            latencies_ms.append(latency_ms)
            order_id = getattr(resp, "order_id", None) or (
                resp.get("order_id") if isinstance(resp, dict) else None
            )
        except Exception as exc:  # noqa: BLE001
            errors += 1
            _log.warning("probe.rest_write.error", iter=i, error=str(exc))
        finally:
            if order_id:
                try:
                    await asyncio.to_thread(rest.underlying.cancel_order, order_id=order_id)
                except Exception:  # noqa: BLE001, S110
                    pass
        # Throttle to ~5 req/s to keep the probe civil.
        await asyncio.sleep(0.2)

    if not latencies_ms:
        return {"samples": samples, "errors": errors, "note": "all requests failed"}
    latencies_ms.sort()
    return {
        "samples": len(latencies_ms),
        "errors": errors,
        "p50_ms": round(statistics.median(latencies_ms), 1),
        "p95_ms": round(latencies_ms[int(0.95 * len(latencies_ms)) - 1], 1),
        "p99_ms": round(latencies_ms[int(0.99 * len(latencies_ms)) - 1], 1),
        "max_ms": round(max(latencies_ms), 1),
    }


async def probe_rest_rate_limit(rest: RestClient) -> dict[str, Any]:
    """Ramp GET /exchange/status rate until 429.

    We try 1, 2, 5, 10, 20, 40 req/s in 3-second bursts. First burst that
    produces a 429 is recorded as the ceiling.
    """
    _log.info("probe.rest_ratelimit.start")
    result: dict[str, Any] = {"rates_tested": [], "limit_hit_at_rps": None, "retry_after_sec": None}
    rates = [1, 2, 5, 10, 20, 40]
    for rps in rates:
        burst_duration = 3.0
        sleep = 1.0 / rps
        errors = 0
        ok = 0
        retry_after: float | None = None
        t_start = time.monotonic()
        while time.monotonic() - t_start < burst_duration:
            t0 = time.monotonic()
            try:
                await asyncio.to_thread(rest.underlying.get_exchange_status)
                ok += 1
            except Exception as exc:  # noqa: BLE001
                errors += 1
                msg = str(exc)
                if "429" in msg:
                    # Try to extract Retry-After if present.
                    import re

                    m = re.search(r"retry[_\- ]after[:=]\s*(\d+(?:\.\d+)?)", msg, re.IGNORECASE)
                    if m:
                        retry_after = float(m.group(1))
                    result["limit_hit_at_rps"] = rps
                    result["retry_after_sec"] = retry_after
                    break
            elapsed = time.monotonic() - t0
            if elapsed < sleep:
                await asyncio.sleep(sleep - elapsed)
        result["rates_tested"].append({"rps": rps, "ok": ok, "errors": errors})
        _log.info("probe.rest_ratelimit.step", rps=rps, ok=ok, errors=errors)
        if result["limit_hit_at_rps"] is not None:
            break
        await asyncio.sleep(2.0)  # cool-down between bursts
    return result


async def probe_end_to_end_loop(rest: RestClient, ticker: str, wait_sec: float = 30.0) -> dict[str, Any]:
    """Measure WS-event → REST-fire round-trip on a real liquid ticker."""
    _log.info("probe.e2e.start", ticker=ticker, wait_sec=wait_sec)
    result: dict[str, Any] = {"events_seen": 0, "orders_fired": 0, "latency_ms": []}
    feed = rest.underlying.feed()
    start = time.monotonic()

    async with feed as f:
        event_queue: asyncio.Queue[tuple[float, Any]] = asyncio.Queue()

        @f.on("orderbook_delta")
        def _cap(msg: Any) -> None:
            event_queue.put_nowait((time.monotonic(), msg))

        f.subscribe("orderbook_delta", market_tickers=[ticker])

        while time.monotonic() - start < wait_sec:
            try:
                event_ts, _msg = await asyncio.wait_for(event_queue.get(), timeout=5.0)
            except TimeoutError:
                continue
            result["events_seen"] += 1
            # Fire a non-filling demo order immediately and measure.
            order_id = None
            try:
                resp = await asyncio.to_thread(
                    rest.underlying.create_order,
                    ticker=ticker,
                    action="buy",
                    side="yes",
                    type="limit",
                    count=1,
                    yes_price=1,
                )
                roundtrip_ms = (time.monotonic() - event_ts) * 1000
                result["latency_ms"].append(round(roundtrip_ms, 1))
                result["orders_fired"] += 1
                order_id = getattr(resp, "order_id", None) or (
                    resp.get("order_id") if isinstance(resp, dict) else None
                )
            except Exception as exc:  # noqa: BLE001
                _log.warning("probe.e2e.fire_failed", error=str(exc))
            finally:
                if order_id:
                    try:
                        await asyncio.to_thread(rest.underlying.cancel_order, order_id=order_id)
                    except Exception:  # noqa: BLE001, S110
                        pass
            # Don't hammer — 1 firing per event is enough; cap at 30 samples.
            if result["orders_fired"] >= 30:
                break

    lats = result.pop("latency_ms")
    if lats:
        lats.sort()
        result["p50_ms"] = round(statistics.median(lats), 1)
        result["p95_ms"] = round(lats[int(0.95 * len(lats)) - 1], 1)
        result["p99_ms"] = round(lats[int(0.99 * len(lats)) - 1], 1)
        result["samples"] = len(lats)
    return result


async def run() -> ProbeResults:
    cfg = Config.load()
    if not cfg.kalshi_use_demo:
        raise RuntimeError(
            "Probe MUST run in demo mode. Set KALSHI_USE_DEMO=true before running."
        )
    if not cfg.kalshi_api_key_id:
        raise RuntimeError("KALSHI_API_KEY_ID not set; create .env first.")

    rest = RestClient(
        RestConfig(
            api_key_id=cfg.kalshi_api_key_id,
            private_key_path=cfg.kalshi_private_key_path,
            use_demo=True,
        )
    )

    # Build a ticker pool from the first whitelisted category that has >=50
    # open markets. Demo may have fewer markets than prod; we adapt.
    prefixes: tuple[str, ...] = ()
    for cat in cfg.universe_categories:
        prefixes = prefixes + CATEGORY_PREFIXES.get(cat, ())
    pool = await asyncio.to_thread(rest.list_open_markets, series_prefixes=prefixes)
    pool_tickers = [m.ticker for m in pool]
    if not pool_tickers:
        # Demo may not have the prefixes; widen to anything open.
        pool = await asyncio.to_thread(rest.list_open_markets)
        pool_tickers = [m.ticker for m in pool][:500]
    _log.info("probe.pool", count=len(pool_tickers))

    results = ProbeResults(
        ts_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        demo_mode=True,
    )
    # 1. WS cap
    results.ws_subscription = await probe_ws_subscription_cap(rest, pool_tickers[:500])

    # 2+4 need a known-liquid ticker. Pick highest 24h volume.
    if pool:
        pool.sort(key=lambda m: m.volume_24h, reverse=True)
        liquid_ticker = pool[0].ticker
    else:
        liquid_ticker = pool_tickers[0] if pool_tickers else ""

    if liquid_ticker:
        results.rest_write_latency_ms = await probe_rest_write_latency(rest, liquid_ticker)
        results.end_to_end_loop_ms = await probe_end_to_end_loop(rest, liquid_ticker)
    else:
        results.notes.append("No liquid ticker available; REST write + E2E probes skipped.")

    # 3. REST rate limit
    results.rest_rate_limit = await probe_rest_rate_limit(rest)

    _write_results(results)

    if cfg.auto_publish:
        _publish(results)

    return results


def _write_results(results: ProbeResults) -> None:
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "ts_utc": results.ts_utc,
        "demo_mode": results.demo_mode,
        "ws_subscription": results.ws_subscription,
        "rest_write_latency_ms": results.rest_write_latency_ms,
        "rest_rate_limit": results.rest_rate_limit,
        "end_to_end_loop_ms": results.end_to_end_loop_ms,
        "notes": results.notes,
    }
    with RESULTS_PATH.open("w") as f:
        yaml.safe_dump(data, f, sort_keys=False)
    _log.info("probe.results_written", path=str(RESULTS_PATH))


def _publish(results: ProbeResults) -> None:
    """Commit detected_limits.yaml to the auto-publish branch.

    We use git directly because the kalshi-arb repo is sitting inside the
    parent Kalshi repo (until the fresh repo is created). The auto-publish
    branch isolates probe output from application code.
    """
    try:
        subprocess.run(["git", "add", str(RESULTS_PATH)], check=True)
        subprocess.run(
            ["git", "commit", "-m", f"probe: detected_limits {results.ts_utc}"],
            check=False,
        )
        branch = Config.load().auto_publish_branch
        subprocess.run(["git", "push", "origin", f"HEAD:{branch}"], check=False)
        _log.info("probe.published", branch=branch)
    except subprocess.CalledProcessError as exc:
        _log.warning("probe.publish_failed", error=str(exc))


if __name__ == "__main__":
    from .. import log as _lg

    _lg.setup()
    asyncio.run(run())
