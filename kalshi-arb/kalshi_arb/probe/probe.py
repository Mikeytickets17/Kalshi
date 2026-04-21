"""One-shot diagnostic probe (demo + prod).

Measures four unknowns and writes them to config/detected_limits.yaml
so the rest of the system sizes itself against real numbers instead of
guesses.

Probes
------
1. WS subscription cap -- how many tickers can ONE WebSocket connection
   subscribe to via orderbook_delta before Kalshi rejects or silently drops.
2. REST write latency distribution -- p50/p95/p99 round-trip for order
   placement. Uses a 1c BUY YES limit that CANNOT fill in any reasonably
   liquid market. Every order is cancelled immediately and tagged with a
   `probe-` COID prefix for audit.
3. REST rate-limit ceiling -- ramp request rate on GET /markets until 429,
   record the ceiling and Retry-After.
4. End-to-end arb loop latency -- WS book update -> REST fire -> response.
   Fires the same unfillable 1c BUY for safety. Skipped in demo mode
   (demo activity is too thin for meaningful numbers).

Strict mode (prod)
------------------
* Every order uses client_order_id = probe-<tag>-<ts_ms>-<iter>.
* An unexpected fill (filled_count > 0 on a 1c BUY) trips a CRITICAL
  log line + cancel + raises ProbeFailure so no yaml is written.
* Any probe that does not satisfy validate_prod_results() aborts the
  run BEFORE detected_limits.yaml is written. Partial results are
  worse than no results.
* Global asyncio.wait_for(timeout_sec=180) wraps the whole suite.

Run
---
    python -m kalshi_arb.cli probe --env demo     # existing behavior
    python -m kalshi_arb.cli probe --env prod     # prod, with gates

Results land at config/detected_limits.yaml. When env=prod and all
checks pass, Paper CLI's startup gate accepts it.
"""

from __future__ import annotations

import asyncio
import re
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

import yaml

from .. import clock, log
from ..config import CATEGORY_PREFIXES, Config
from ..rest.client import RestClient, RestConfig
from .analysis import (
    ERROR_DETAIL_PREFIX,
    PROBE_COID_PREFIX,
    ProbeResults,
    build_error_detail_lines,
    build_failed_detail_lines,
    build_summary_line,
    build_yaml_body,
    probe_coid,
    rate_limit_summary,
    validate_prod_results,
)

_log = log.get("probe")

RESULTS_PATH = Path("config/detected_limits.yaml")

DEFAULT_TIMEOUT_SEC = 180.0   # 3-minute hard ceiling per operator spec

DEMO_NOTES = (
    "Measured in demo environment. Production values may differ -- especially "
    "REST rate-limit ceiling (production is tier-gated) and message volume "
    "(production markets have vastly higher activity)."
)
PROD_NOTES = (
    "Measured in production environment. No account-identifying data emitted; "
    "all orders were 1c BUY YES limits that cannot fill in liquid markets, "
    "cancelled immediately, and COID-tagged with the 'probe-' prefix for audit."
)


class ProbeFailure(RuntimeError):
    """Raised when any probe's strict-mode acceptance criterion fails.

    Callers MUST NOT write detected_limits.yaml when this is raised --
    partial results are worse than no results (paper CLI's startup gate
    would accept a broken probe file)."""


def _ioc() -> Any:
    """Return pykalshi's TimeInForce.IOC. Lazy import so tests + CI
    environments without pykalshi can still import this module."""
    from pykalshi.enums import TimeInForce

    return TimeInForce.IOC


# ---------------------------------------------------------------------
# Per-probe transport interface. Real impl wraps pykalshi; test impl is
# a deterministic in-memory fake. See `RealProbeTransport` below and
# FakeProbeTransport in tests/test_probe_suite.py.
# ---------------------------------------------------------------------


class ProbeTransport(Protocol):
    async def list_open_markets(
        self, series_prefixes: tuple[str, ...] = (), limit: int = 1000
    ) -> list[Any]:
        ...

    async def ws_subscription_cap(
        self, tickers: list[str], *, step_size: int = 50
    ) -> dict[str, Any]:
        ...

    async def rest_write_latency(
        self, ticker: str, samples: int, *, coid_tag: str
    ) -> dict[str, Any]:
        ...

    async def rest_rate_limit(self) -> dict[str, Any]:
        ...

    async def end_to_end_loop(
        self, ticker: str, wait_sec: float, *, coid_tag: str
    ) -> dict[str, Any]:
        ...


# ---------------------------------------------------------------------
# Real implementation -- wraps the pykalshi REST + WS clients.
# ---------------------------------------------------------------------


@dataclass
class RealProbeTransport:
    rest: RestClient

    async def list_open_markets(
        self, series_prefixes: tuple[str, ...] = (), limit: int = 1000
    ) -> list[Any]:
        return await asyncio.to_thread(
            self.rest.list_open_markets,
            series_prefixes=series_prefixes,
            limit=limit,
        )

    async def ws_subscription_cap(
        self, tickers: list[str], *, step_size: int = 50
    ) -> dict[str, Any]:
        _log.info("probe.ws.start", ceiling=len(tickers))
        result: dict[str, Any] = {
            "max_confirmed_tickers": 0,
            "failed_at_tickers": None,
            "failure_mode": None,
            "steps": [],
        }
        feed = self.rest.async_underlying().feed()
        msg_counter: dict[str, int] = {}

        # Build the sequence of step sizes to probe. Historically this
        # was `range(step_size, len(tickers)+1, step_size)` which is
        # EMPTY when the pool is smaller than step_size (e.g., a
        # 40-ticker crypto-only universe under a 50-step). Empty loop
        # => max_confirmed_tickers stays at 0 => threshold gate fails
        # with an opaque "< required" message even though the real
        # problem is "not enough markets in the whitelist". The fix:
        # always include the full pool size as the terminal checkpoint,
        # and always include at least one checkpoint if tickers exist.
        n = len(tickers)
        checkpoints: list[int] = list(range(step_size, n + 1, step_size))
        if n > 0 and (not checkpoints or checkpoints[-1] != n):
            checkpoints.append(n)
        # Dedupe + sort defensively.
        checkpoints = sorted(set(checkpoints))

        async with feed as f:
            @f.on("orderbook_delta")
            def _count(msg: Any) -> None:
                t = getattr(msg, "market_ticker", None) or (
                    msg.get("market_ticker") if isinstance(msg, dict) else None
                )
                if t:
                    msg_counter[t] = msg_counter.get(t, 0) + 1

            for upper in checkpoints:
                slice_ = tickers[:upper]
                try:
                    f.subscribe("orderbook_delta", market_tickers=slice_)
                except Exception as exc:  # noqa: BLE001
                    result["failed_at_tickers"] = upper
                    result["failure_mode"] = f"subscribe_error: {exc}"
                    _log.warning(
                        "probe.ws.subscribe_rejected", at=upper, error=str(exc)
                    )
                    break

                wait_start = time.monotonic()
                while time.monotonic() - wait_start < 10.0:
                    active = sum(1 for t in slice_ if msg_counter.get(t))
                    if active >= max(1, len(slice_) // 4):
                        break
                    await asyncio.sleep(0.5)

                active = sum(1 for t in slice_ if msg_counter.get(t))
                result["steps"].append({
                    "subscribed": upper,
                    "receiving_messages": active,
                    "coverage_pct": round(100 * active / max(1, upper), 1),
                })
                # Silent drop-off detection only applies once we're
                # above the first checkpoint and have step headroom
                # to compare against. A sub-step pool (e.g. 40 tickers
                # with step=50) doesn't qualify for this check.
                if upper > step_size and active < upper * 0.25:
                    result["failed_at_tickers"] = upper
                    result["failure_mode"] = f"silent_drop_off at {active}/{upper}"
                    break
                result["max_confirmed_tickers"] = upper
        return result

    async def rest_write_latency(
        self, ticker: str, samples: int, *, coid_tag: str
    ) -> dict[str, Any]:
        """Fire unfillable 1c BUY YES orders, measure placement round-trip,
        cancel every order. Unexpected fill -> CRITICAL + raise.

        Uses pykalshi's canonical API surface:
          * client.portfolio.place_order(...) -- not the long-assumed
            (and non-existent) client.create_order(...)
          * count_fp: fixed-point decimal string (e.g. "1")
          * yes_price_dollars: dollar string (e.g. "0.01") -- pykalshi
            does NOT accept integer-cent inputs
          * time_in_force: IOC so a miss auto-cancels, but we still
            call portfolio.cancel_order() in finally for belt-and-
            suspenders against any resting state.
        """
        from ..common.errors import ErrorCapture

        _log.info("probe.rest_write.start", samples=samples, ticker=ticker)
        latencies_ms: list[float] = []
        errors = ErrorCapture(max_unique=5)
        unexpected_fills = 0

        for i in range(samples):
            coid = probe_coid(clock.now_ms(), i, tag=coid_tag)
            t0 = time.monotonic()
            order_id = None
            filled = 0
            try:
                resp = await asyncio.to_thread(
                    self.rest.underlying.portfolio.place_order,
                    ticker=ticker,
                    action="buy",
                    side="yes",
                    count_fp="1",
                    yes_price_dollars="0.01",
                    client_order_id=coid,
                    time_in_force=_ioc(),
                )
                latency_ms = (time.monotonic() - t0) * 1000
                latencies_ms.append(latency_ms)
                errors.record_success()
                order_id = getattr(resp, "order_id", None) or (
                    resp.get("order_id") if isinstance(resp, dict) else None
                )
                filled = int(getattr(resp, "filled_count", 0) or (
                    resp.get("filled_count", 0) if isinstance(resp, dict) else 0
                ))
            except Exception as exc:  # noqa: BLE001
                errors.record(exc, context={
                    "iter": i, "ticker": ticker, "coid": coid,
                })
            finally:
                if order_id:
                    try:
                        await asyncio.to_thread(
                            self.rest.underlying.portfolio.cancel_order,
                            order_id=order_id,
                        )
                    except Exception:  # noqa: BLE001, S110
                        pass
            if filled > 0:
                unexpected_fills += 1
                _log.critical(
                    "probe.unexpected_fill",
                    coid=coid,
                    ticker=ticker,
                    filled=filled,
                    detail=(
                        "A 1c BUY YES limit order filled. Either the market "
                        "is pathologically illiquid or a seller is giving "
                        "contracts away at 1c. Cancelling and aborting."
                    ),
                )
            await asyncio.sleep(0.2)

        if unexpected_fills > 0:
            raise ProbeFailure(
                f"rest_write_latency: {unexpected_fills} unfillable orders "
                f"filled on {ticker}. Aborting -- see CRITICAL log lines."
            )

        if not latencies_ms:
            return {
                "samples": samples,
                "successful": 0,
                "errors_summary": errors.to_dict(),
                "note": "all requests failed -- see errors_summary for cause",
            }
        latencies_ms.sort()
        n = len(latencies_ms)
        return {
            "samples": samples,
            "successful": n,
            "errors_summary": errors.to_dict(),
            "p50_ms": round(statistics.median(latencies_ms), 1),
            "p95_ms": round(latencies_ms[max(0, int(0.95 * n) - 1)], 1),
            "p99_ms": round(latencies_ms[max(0, int(0.99 * n) - 1)], 1),
            "max_ms": round(max(latencies_ms), 1),
        }

    async def rest_rate_limit(self) -> dict[str, Any]:
        from ..common.errors import ErrorCapture

        _log.info("probe.rest_ratelimit.start")
        errors = ErrorCapture(max_unique=5)
        result: dict[str, Any] = {
            "endpoint": "/markets?limit=1",
            "rates_tested": [],
            "limit_hit_at_rps": None,
            "retry_after_sec": None,
            "errors_summary": None,
            "max_successful_rps": 0,
        }
        rates = [1, 2, 5, 10, 20, 40]
        for rps in rates:
            burst_duration = 3.0
            sleep = 1.0 / rps
            e_count = 0
            ok = 0
            retry_after: float | None = None
            t_start = time.monotonic()
            while time.monotonic() - t_start < burst_duration:
                t0 = time.monotonic()
                try:
                    await asyncio.to_thread(
                        self.rest.underlying.get_markets,
                        limit=1, fetch_all=False,
                    )
                    ok += 1
                    errors.record_success()
                except Exception as exc:  # noqa: BLE001
                    e_count += 1
                    errors.record(exc, context={"rps": rps})
                    msg = str(exc)
                    if "429" in msg:
                        m = re.search(
                            r"retry[_\- ]after[:=]\s*(\d+(?:\.\d+)?)",
                            msg, re.IGNORECASE,
                        )
                        if m:
                            retry_after = float(m.group(1))
                        result["limit_hit_at_rps"] = rps
                        result["retry_after_sec"] = retry_after
                        break
                elapsed = time.monotonic() - t0
                if elapsed < sleep:
                    await asyncio.sleep(sleep - elapsed)
            step = {"rps": rps, "ok": ok, "errors": e_count}
            result["rates_tested"].append(step)
            if e_count == 0:
                result["max_successful_rps"] = rps
            if result["limit_hit_at_rps"] is not None:
                break
            await asyncio.sleep(2.0)
        result["errors_summary"] = errors.to_dict()
        # Collapse the ramp into a summary for the yaml.
        summary = rate_limit_summary(result["rates_tested"])
        result["max_successful_rps"] = summary["max_successful_rps"]
        if result["limit_hit_at_rps"] is None:
            result["limit_hit_at_rps"] = summary["limit_hit_at_rps"]
        return result

    async def end_to_end_loop(
        self, ticker: str, wait_sec: float, *, coid_tag: str
    ) -> dict[str, Any]:
        """Measure WS-event -> REST-fire round-trip on a live ticker.

        Fires the same unfillable 1c BUY YES order as rest_write_latency
        so we don't leak capital if something goes wrong. Caps at 30
        samples to bound exposure even in chatty markets.

        Every fire-attempt is tracked in an ErrorCapture so the
        per-group 'REST WRITE failures: Nx HTTP 403 ...' diagnostic
        also applies to the e2e loop. Previously this probe just
        _log.warning'd errors and dropped them on the floor, which
        left the operator guessing when Kalshi rejected every
        order-fire in a row."""
        from ..common.errors import ErrorCapture

        _log.info("probe.e2e.start", ticker=ticker, wait_sec=wait_sec)
        latencies_ms: list[float] = []
        events_seen = 0
        orders_fired = 0
        unexpected_fills = 0
        errors = ErrorCapture(max_unique=5)

        feed = self.rest.async_underlying().feed()
        start = time.monotonic()

        async with feed as f:
            event_queue: asyncio.Queue[tuple[float, Any]] = asyncio.Queue()

            @f.on("orderbook_delta")
            def _cap(msg: Any) -> None:
                event_queue.put_nowait((time.monotonic(), msg))

            f.subscribe("orderbook_delta", market_tickers=[ticker])

            while time.monotonic() - start < wait_sec:
                try:
                    event_ts, _msg = await asyncio.wait_for(
                        event_queue.get(), timeout=5.0
                    )
                except TimeoutError:
                    continue
                events_seen += 1
                coid = probe_coid(clock.now_ms(), events_seen, tag=coid_tag)
                order_id = None
                filled = 0
                try:
                    resp = await asyncio.to_thread(
                        self.rest.underlying.portfolio.place_order,
                        ticker=ticker,
                        action="buy",
                        side="yes",
                        count_fp="1",
                        yes_price_dollars="0.01",
                        client_order_id=coid,
                        time_in_force=_ioc(),
                    )
                    roundtrip_ms = (time.monotonic() - event_ts) * 1000
                    latencies_ms.append(roundtrip_ms)
                    orders_fired += 1
                    errors.record_success()
                    order_id = getattr(resp, "order_id", None) or (
                        resp.get("order_id") if isinstance(resp, dict) else None
                    )
                    filled = int(getattr(resp, "filled_count", 0) or (
                        resp.get("filled_count", 0) if isinstance(resp, dict) else 0
                    ))
                except Exception as exc:  # noqa: BLE001
                    errors.record(exc, context={
                        "iter": events_seen, "ticker": ticker, "coid": coid,
                    })
                    _log.warning("probe.e2e.fire_failed", error=str(exc))
                finally:
                    if order_id:
                        try:
                            await asyncio.to_thread(
                                self.rest.underlying.portfolio.cancel_order,
                                order_id=order_id,
                            )
                        except Exception:  # noqa: BLE001, S110
                            pass
                if filled > 0:
                    unexpected_fills += 1
                    _log.critical(
                        "probe.unexpected_fill",
                        coid=coid,
                        ticker=ticker,
                        filled=filled,
                        detail="e2e probe 1c BUY filled -- aborting run.",
                    )
                if orders_fired >= 30:
                    break

        if unexpected_fills > 0:
            raise ProbeFailure(
                f"end_to_end_loop: {unexpected_fills} unfillable orders "
                f"filled on {ticker}. Aborting."
            )

        result: dict[str, Any] = {
            "events_seen": events_seen,
            "orders_fired": orders_fired,
            "errors_summary": errors.to_dict(),
        }
        if latencies_ms:
            latencies_ms.sort()
            n = len(latencies_ms)
            result["samples"] = n
            result["p50_ms"] = round(statistics.median(latencies_ms), 1)
            result["p95_ms"] = round(latencies_ms[max(0, int(0.95 * n) - 1)], 1)
            result["p99_ms"] = round(latencies_ms[max(0, int(0.99 * n) - 1)], 1)
            result["max_ms"] = round(max(latencies_ms), 1)
        return result


# ---------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------


async def run(
    env: str = "demo",
    *,
    transport: ProbeTransport | None = None,
    rest: RestClient | None = None,
    universe_categories: list[str] | None = None,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    write_path: Path = RESULTS_PATH,
    write_enabled: bool = True,
    e2e_wait_sec: float = 30.0,
    rest_write_samples: int = 100,
) -> ProbeResults:
    """Run the four probes. Return a fully-populated ProbeResults.

    env='prod':
      * Requires KALSHI_USE_DEMO=false (enforced in CLI before this runs).
      * Runs all four probes, including E2E.
      * Applies validate_prod_results(); raises ProbeFailure on any miss.
      * Writes detected_limits.yaml only on pass.

    env='demo':
      * Runs WS + REST write + rate-limit probes.
      * Defers E2E (demo activity too thin).
      * Writes detected_limits.yaml regardless of strict thresholds
        (demo numbers are informational).

    `transport` (test injection) or `rest` (real run) -- exactly one
    must be provided. `universe_categories` defaults to the loaded
    Config so tests can pin it.
    """
    if transport is None and rest is None:
        cfg = Config.load()
        rest = RestClient(
            RestConfig(
                api_key_id=cfg.kalshi_api_key_id,
                private_key_path=cfg.kalshi_private_key_path,
                use_demo=cfg.kalshi_use_demo,
            )
        )
        if universe_categories is None:
            universe_categories = list(cfg.universe_categories)

    if transport is None:
        assert rest is not None
        transport = RealProbeTransport(rest=rest)

    if universe_categories is None:
        universe_categories = ["crypto", "weather", "econ"]

    results = ProbeResults(
        ts_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        environment=env,
    )

    try:
        await asyncio.wait_for(
            _run_suite(
                transport=transport,
                env=env,
                results=results,
                universe_categories=universe_categories,
                e2e_wait_sec=e2e_wait_sec,
                rest_write_samples=rest_write_samples,
            ),
            timeout=timeout_sec,
        )
    except asyncio.TimeoutError as exc:
        # Surface whatever we managed to measure before emitting the
        # failure so the operator can see partial numbers even on
        # timeout. `build_summary_line` handles missing fields.
        _emit_summary_line(results)
        raise ProbeFailure(
            f"probe suite exceeded {timeout_sec}s timeout"
        ) from exc

    # Always print the measurement summary, whether we PASS or FAIL.
    # This is the line verify_prod_probe.bat pulls into the popup so
    # the operator sees every measured number at a glance.
    _emit_summary_line(results)

    # ALWAYS emit grouped Kalshi error responses for any probe block
    # that captured write failures, regardless of env. Demo runs skip
    # strict threshold validation (demo yamls are informational), but
    # the operator still needs to see 'REST WRITE 100x HTTP 403 ...'
    # in the console when 100/100 writes fail. Previously these lines
    # only landed on prod+failure, which hid the root cause when the
    # same rejection pattern surfaced on demo.
    error_details = build_error_detail_lines(results)
    for reason in error_details:
        sys.stderr.write(ERROR_DETAIL_PREFIX + reason + "\n")
    if error_details:
        sys.stderr.flush()

    # Strict acceptance in prod only. Demo yamls are informational and
    # get written as-is.
    if env == "prod":
        failures = validate_prod_results(results)
        if failures:
            # Threshold-miss lines use the stable PROBE FAILED DETAIL
            # prefix the .bat already grep's for. ERROR_DETAIL lines
            # above cover 'why Kalshi rejected'; FAIL_DETAIL covers
            # 'which gate blocked the run'. Two prefixes, two concerns.
            for line in build_failed_detail_lines(failures):
                sys.stderr.write(line + "\n")
            sys.stderr.flush()
            raise ProbeFailure(
                "prod probe thresholds not met:\n  - "
                + "\n  - ".join(failures)
                + ("\n\nKalshi error responses:\n  - "
                   + "\n  - ".join(error_details) if error_details else "")
            )

    _scrub(results)
    if write_enabled:
        _write_results(results, path=write_path)
    return results


def _emit_summary_line(results: ProbeResults) -> None:
    """Print the single-line measurement snapshot to stderr. Safe to
    call with an incomplete ProbeResults (missing fields render as
    'n/a'). Called on success, failure, AND timeout."""
    line = build_summary_line(results)
    sys.stderr.write(line + "\n")
    sys.stderr.flush()
    _log.info("probe.summary", line=line)


async def _run_suite(
    *,
    transport: ProbeTransport,
    env: str,
    results: ProbeResults,
    universe_categories: list[str],
    e2e_wait_sec: float,
    rest_write_samples: int,
) -> None:
    # Build the ticker pool.
    prefixes: tuple[str, ...] = ()
    for cat in universe_categories:
        prefixes = prefixes + CATEGORY_PREFIXES.get(cat, ())

    pool = await transport.list_open_markets(series_prefixes=prefixes, limit=1000)
    pool_tickers = [getattr(m, "ticker", None) or m.get("ticker", "") for m in pool]
    pool_tickers = [t for t in pool_tickers if t]

    if not pool_tickers:
        pool = await transport.list_open_markets(limit=500)
        pool_tickers = [getattr(m, "ticker", None) or m.get("ticker", "") for m in pool]
        pool_tickers = [t for t in pool_tickers if t]
        results.notes.append(
            f"Category whitelist {universe_categories} returned 0 markets; "
            f"fell back to {len(pool_tickers)} open markets of any kind."
        )
    _log.info("probe.pool", count=len(pool_tickers))

    # Pick the most liquid ticker for write + E2E probes.
    liquid_ticker = ""
    if pool:
        by_volume = sorted(
            pool,
            key=lambda m: getattr(m, "volume_24h", 0) or (
                m.get("volume_24h", 0) if isinstance(m, dict) else 0
            ),
            reverse=True,
        )
        liquid_ticker = (
            getattr(by_volume[0], "ticker", "")
            or (by_volume[0].get("ticker", "") if isinstance(by_volume[0], dict) else "")
        )
    if not liquid_ticker and pool_tickers:
        liquid_ticker = pool_tickers[0]

    # 1. WS subscription cap
    results.ws_subscription = await transport.ws_subscription_cap(pool_tickers[:500])

    # 2. REST write latency
    if liquid_ticker:
        results.rest_write_latency_ms = await transport.rest_write_latency(
            liquid_ticker, rest_write_samples, coid_tag="write",
        )
    else:
        results.rest_write_latency_ms = {
            "samples": 0, "successful": 0,
            "note": "no liquid ticker available -- REST write probe skipped",
        }

    # 3. REST rate limit
    results.rest_rate_limit = await transport.rest_rate_limit()

    # 4. End-to-end loop. Only runs in prod; demo is too thin.
    if env == "prod" and liquid_ticker:
        results.end_to_end_loop_ms = await transport.end_to_end_loop(
            liquid_ticker, wait_sec=e2e_wait_sec, coid_tag="e2e",
        )
    else:
        results.end_to_end_loop_ms = {
            "status": "deferred",
            "reason": "demo_activity_too_thin" if env == "demo" else "no_liquid_ticker",
            "note": (
                "Demo market activity is too thin for a meaningful end-to-end "
                "measurement. Run in prod to capture."
            ),
        }

    _annotate(results, env=env)


def _annotate(results: ProbeResults, *, env: str) -> None:
    note = PROD_NOTES if env == "prod" else DEMO_NOTES
    for block_name in (
        "ws_subscription",
        "rest_write_latency_ms",
        "rest_rate_limit",
        "end_to_end_loop_ms",
    ):
        block = getattr(results, block_name)
        if isinstance(block, dict):
            block["environment"] = env
            block.setdefault("notes", note)


def _scrub(results: ProbeResults) -> None:
    """Strip any key/account/header material from the probe output.

    Belt-and-suspenders: probes never collect these on purpose but
    we redact anything that looks like a credential before write."""
    patterns = [
        re.compile(r"[A-Fa-f0-9]{24,}"),           # long hex IDs
        re.compile(r"Bearer\s+\S+", re.IGNORECASE),
        re.compile(r"KALSHI[-_]API[-_]KEY", re.IGNORECASE),
    ]
    # Probe COIDs are deliberately emitted and safe -- whitelist them.
    redacted_any = False

    def scrub_value(v: Any) -> Any:
        nonlocal redacted_any
        if isinstance(v, str):
            if v.startswith(PROBE_COID_PREFIX):
                return v
            for p in patterns:
                if p.search(v):
                    redacted_any = True
                    return "<redacted>"
            return v
        if isinstance(v, dict):
            return {k: scrub_value(vv) for k, vv in v.items()}
        if isinstance(v, list):
            return [scrub_value(x) for x in v]
        return v

    for block_name in (
        "ws_subscription",
        "rest_write_latency_ms",
        "rest_rate_limit",
        "end_to_end_loop_ms",
    ):
        block = getattr(results, block_name)
        if isinstance(block, dict):
            setattr(results, block_name, scrub_value(block))
    if redacted_any:
        results.notes.append("Some values were redacted by the scrub guard.")


def _write_results(results: ProbeResults, *, path: Path = RESULTS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = build_yaml_body(results)
    with path.open("w") as f:
        yaml.safe_dump(data, f, sort_keys=False)
    _log.info("probe.results_written", path=str(path))


def _publish(results: ProbeResults, *, path: Path = RESULTS_PATH) -> None:
    """Commit detected_limits.yaml to the auto-publish branch."""
    try:
        subprocess.run(["git", "add", str(path)], check=True)
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
    asyncio.run(run(env="demo"))
