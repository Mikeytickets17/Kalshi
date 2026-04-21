"""Tests that pin the diagnostic UX contract.

These tests cover the failure-reproduction path the operator hit in
production: every probe returned 200 OK / WS connected / no auth
errors, yet the run reported "thresholds not met" with no detail.

Invariants:
  1. Every probe run (PASS, FAIL, or TIMEOUT) emits exactly one
     `PROBE SUMMARY:` line to stderr before exit.
  2. On threshold failure, every specific reason is emitted as its
     own `PROBE FAILED DETAIL:` stderr line AND included in the
     ProbeFailure message.
  3. The WS subscription-cap probe does NOT report 0 when the pool
     is smaller than step_size (regression that hid positive results
     in narrow category whitelists).
  4. The calibrated prod thresholds (WS >= 10, rate >= 3 rps, e2e
     samples >= 3) match the constants in analysis.py.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from kalshi_arb.probe.analysis import (
    FAIL_DETAIL_PREFIX,
    PROD_MIN_E2E_SAMPLES,
    PROD_MIN_RATE_RPS,
    PROD_MIN_WRITE_SUCCESS_PCT,
    PROD_MIN_WS_CONFIRMED_TICKERS,
    SUMMARY_PREFIX,
    ProbeResults,
    build_failed_detail_lines,
    build_summary_line,
    validate_prod_results,
)
from kalshi_arb.probe.probe import ProbeFailure, run


REPO_ROOT = Path(__file__).resolve().parents[1]


# ----- build_summary_line: always populated, handles missing ------


def test_summary_line_fully_populated_shape():
    r = ProbeResults(
        ts_utc="2026-04-20T16:00:00Z",
        environment="prod",
        ws_subscription={"max_confirmed_tickers": 42},
        rest_write_latency_ms={
            "samples": 100, "successful": 95,
            "p50_ms": 50.0, "p95_ms": 120.0,
        },
        rest_rate_limit={"max_successful_rps": 5},
        end_to_end_loop_ms={"samples": 7},
    )
    line = build_summary_line(r)
    assert line.startswith(SUMMARY_PREFIX)
    assert "env=prod" in line
    assert "ws_cap=42" in line
    assert "rest_write_p50=50.0ms" in line
    assert "rest_write_p95=120.0ms" in line
    assert "rest_write_ok_rate=0.95" in line
    assert "rate_ceiling=5rps" in line
    assert "e2e_samples=7" in line


def test_summary_line_marks_missing_fields_as_na():
    """A timeout that fires before any probe completes still emits a
    summary line -- the operator gets 'every field n/a' which is the
    correct diagnostic signal."""
    r = ProbeResults(environment="prod")
    line = build_summary_line(r)
    assert line.startswith(SUMMARY_PREFIX)
    assert "ws_cap=n/a" in line
    assert "rest_write_p50=n/a" in line
    assert "rest_write_p95=n/a" in line
    assert "rest_write_ok_rate=n/a" in line
    assert "rate_ceiling=n/a" in line
    assert "e2e_samples=n/a" in line


def test_summary_line_is_one_physical_line():
    r = ProbeResults(environment="prod")
    line = build_summary_line(r)
    assert "\n" not in line, "summary line must fit on one stdio line"


# ----- build_failed_detail_lines -----------------------------------


def test_failed_detail_lines_prefix_each_reason():
    reasons = [
        "ws_subscription: max_confirmed_tickers=5 < required 10",
        "rest_rate_limit: max_successful_rps=1 < required 3",
    ]
    lines = build_failed_detail_lines(reasons)
    assert len(lines) == 2
    assert all(line.startswith(FAIL_DETAIL_PREFIX) for line in lines)
    assert FAIL_DETAIL_PREFIX + reasons[0] == lines[0]
    assert FAIL_DETAIL_PREFIX + reasons[1] == lines[1]


def test_failed_detail_lines_empty_input_returns_empty():
    assert build_failed_detail_lines([]) == []


# ----- Threshold constants match the calibrated production floor ---


def test_calibrated_thresholds_reflect_prod_reality():
    """These are the operator-facing acceptance floors. If someone
    tightens them back to the original guesses (50/10/5), this test
    catches the regression so the diagnostic failure messages don't
    turn opaque again on public-tier Kalshi keys."""
    assert PROD_MIN_WS_CONFIRMED_TICKERS == 10
    assert PROD_MIN_RATE_RPS == 3
    assert PROD_MIN_E2E_SAMPLES == 3
    assert PROD_MIN_WRITE_SUCCESS_PCT == 80.0


# ----- validate_prod_results produces measured-vs-required strings -


def test_validate_failure_strings_include_measured_and_required():
    r = ProbeResults(
        environment="prod",
        ws_subscription={"max_confirmed_tickers": 2},
        rest_write_latency_ms={
            "samples": 100, "successful": 50,
            "p50_ms": 1.0,
        },
        rest_rate_limit={"max_successful_rps": 1},
        end_to_end_loop_ms={"samples": 0},
    )
    reasons = validate_prod_results(r)

    # Every reason carries BOTH the measured value AND the requirement
    # so the .bat popup lines are self-describing.
    joined = "\n".join(reasons)
    assert "max_confirmed_tickers=2" in joined
    assert f"< required {PROD_MIN_WS_CONFIRMED_TICKERS}" in joined
    assert "success_rate=50.0%" in joined
    assert f"< required {PROD_MIN_WRITE_SUCCESS_PCT}%" in joined
    assert "max_successful_rps=1" in joined
    assert f"< required {PROD_MIN_RATE_RPS}" in joined
    assert "samples=0" in joined
    assert f"< required {PROD_MIN_E2E_SAMPLES}" in joined


# ----- End-to-end: run() emits summary + detail to stderr ----------


@dataclass
class _RealisticProdTransport:
    """Simulates the operator's observed conditions: every REST call
    returns 200 OK quickly, WS connects fine, no auth / rate-limit
    errors -- and yet one or more thresholds fail. Used to reproduce
    the original opacity bug."""

    max_confirmed_tickers: int = 30
    rest_write_samples: int = 100
    rest_write_successful: int = 100
    rest_write_p95_ms: float = 120.0
    max_successful_rps: int = 5
    e2e_samples: int = 5

    async def list_open_markets(self, series_prefixes=(), limit=1000):
        # Realistic prod crypto+weather+econ universe.
        return [
            _Market(ticker=f"KXBTC-PROD-{i:03d}", volume_24h=5000 - i * 10)
            for i in range(60)
        ]

    async def ws_subscription_cap(self, tickers, *, step_size=50):
        return {
            "max_confirmed_tickers": self.max_confirmed_tickers,
            "failed_at_tickers": None,
            "failure_mode": None,
            "steps": [{"subscribed": self.max_confirmed_tickers,
                       "receiving_messages": self.max_confirmed_tickers,
                       "coverage_pct": 100.0}],
        }

    async def rest_write_latency(self, ticker, samples, *, coid_tag):
        return {
            "samples": self.rest_write_samples,
            "successful": self.rest_write_successful,
            "errors_summary": {"unique_errors": []},
            "p50_ms": 50.0,
            "p95_ms": self.rest_write_p95_ms,
            "p99_ms": 200.0,
            "max_ms": 250.0,
        }

    async def rest_rate_limit(self):
        return {
            "endpoint": "/markets?limit=1",
            "rates_tested": [
                {"rps": 1, "ok": 3, "errors": 0},
                {"rps": 2, "ok": 6, "errors": 0},
                {"rps": 5, "ok": 15, "errors": 0},
            ],
            "limit_hit_at_rps": None,
            "max_successful_rps": self.max_successful_rps,
        }

    async def end_to_end_loop(self, ticker, wait_sec, *, coid_tag):
        return {
            "events_seen": self.e2e_samples * 2,
            "orders_fired": self.e2e_samples,
            "samples": self.e2e_samples,
            "p50_ms": 60.0,
            "p95_ms": 120.0,
        }


@dataclass
class _Market:
    ticker: str
    volume_24h: float = 1000.0


def _run_and_capture(transport, env="prod", **kwargs) -> tuple[ProbeResults | Exception, str]:
    """Run the probe and return (result-or-exception, captured stderr)."""
    import io
    from contextlib import redirect_stderr

    buf = io.StringIO()
    outcome: ProbeResults | Exception
    with redirect_stderr(buf):
        try:
            outcome = asyncio.run(
                run(env=env, transport=transport,
                    universe_categories=["crypto"],
                    e2e_wait_sec=0.1,
                    **kwargs)
            )
        except Exception as exc:  # noqa: BLE001
            outcome = exc
    return outcome, buf.getvalue()


def test_success_run_emits_summary_line_to_stderr(tmp_path):
    """Happy path: every threshold passes -> yaml is written AND
    stderr contains a PROBE SUMMARY line with every measured number."""
    transport = _RealisticProdTransport(
        max_confirmed_tickers=60,  # >=10
        max_successful_rps=5,      # >=3
        e2e_samples=5,             # >=3
    )
    out = tmp_path / "detected_limits.yaml"
    outcome, stderr = _run_and_capture(
        transport, write_path=out,
    )
    assert isinstance(outcome, ProbeResults)
    assert out.exists()
    # Exactly one summary line, regardless of logging volume.
    summary_lines = [l for l in stderr.splitlines() if l.startswith(SUMMARY_PREFIX)]
    assert len(summary_lines) == 1, (
        f"expected 1 summary line, got {len(summary_lines)}:\n{stderr}"
    )
    summary = summary_lines[0]
    assert "ws_cap=60" in summary
    assert "rate_ceiling=5rps" in summary
    assert "e2e_samples=5" in summary


def test_failure_run_emits_summary_plus_failed_detail_per_threshold(tmp_path):
    """Regression guard for the opacity bug: every threshold that
    misses must appear on its own PROBE FAILED DETAIL: stderr line AND
    the always-printed PROBE SUMMARY: line must precede."""
    transport = _RealisticProdTransport(
        max_confirmed_tickers=3,   # < 10
        max_successful_rps=1,      # < 3
        e2e_samples=1,             # < 3
        # REST write is fine; only the three above should fail.
    )
    out = tmp_path / "detected_limits.yaml"
    outcome, stderr = _run_and_capture(transport, write_path=out)

    assert isinstance(outcome, ProbeFailure)
    assert not out.exists(), "detected_limits.yaml must not be written on failure"

    summary_lines = [l for l in stderr.splitlines() if l.startswith(SUMMARY_PREFIX)]
    assert len(summary_lines) == 1, (
        f"summary line missing or duplicated:\n{stderr}"
    )
    summary = summary_lines[0]
    # Measured values MUST be visible even though the run failed.
    assert "ws_cap=3" in summary
    assert "rate_ceiling=1rps" in summary
    assert "e2e_samples=1" in summary

    detail_lines = [
        l for l in stderr.splitlines() if l.startswith(FAIL_DETAIL_PREFIX)
    ]
    # Three thresholds failed -> three detail lines.
    assert len(detail_lines) == 3, (
        f"expected 3 detail lines, got {len(detail_lines)}:\n"
        + "\n".join(detail_lines)
    )
    joined = "\n".join(detail_lines)
    assert "ws_subscription" in joined
    assert "max_confirmed_tickers=3" in joined
    assert "< required 10" in joined
    assert "rest_rate_limit" in joined
    assert "max_successful_rps=1" in joined
    assert "< required 3" in joined
    assert "end_to_end_loop_ms" in joined
    assert "samples=1" in joined
    assert "< required 3" in joined


def test_single_failing_threshold_emits_one_detail_line(tmp_path):
    """Common real-world case: one narrow miss, not a cascade."""
    transport = _RealisticProdTransport(
        max_confirmed_tickers=50,  # ok
        max_successful_rps=5,       # ok
        e2e_samples=1,              # < 3, only this fails
    )
    out = tmp_path / "detected_limits.yaml"
    outcome, stderr = _run_and_capture(transport, write_path=out)

    assert isinstance(outcome, ProbeFailure)
    detail_lines = [
        l for l in stderr.splitlines() if l.startswith(FAIL_DETAIL_PREFIX)
    ]
    assert len(detail_lines) == 1, detail_lines
    assert "end_to_end_loop_ms" in detail_lines[0]
    assert "samples=1" in detail_lines[0]


def test_timeout_still_emits_summary(tmp_path):
    """If the suite times out mid-run, the operator should still see
    a PROBE SUMMARY: line with n/a for unmeasured fields."""

    @dataclass
    class _HangTransport:
        async def list_open_markets(self, series_prefixes=(), limit=1000):
            return [_Market(ticker="KXBTC-0", volume_24h=1)]

        async def ws_subscription_cap(self, tickers, *, step_size=50):
            await asyncio.sleep(5.0)
            return {"max_confirmed_tickers": 0}

        async def rest_write_latency(self, ticker, samples, *, coid_tag):
            return {}

        async def rest_rate_limit(self):
            return {}

        async def end_to_end_loop(self, ticker, wait_sec, *, coid_tag):
            return {}

    out = tmp_path / "detected_limits.yaml"
    outcome, stderr = _run_and_capture(
        _HangTransport(),
        write_path=out,
        timeout_sec=0.3,
    )
    assert isinstance(outcome, ProbeFailure)
    assert "timeout" in str(outcome).lower()
    summary_lines = [l for l in stderr.splitlines() if l.startswith(SUMMARY_PREFIX)]
    assert len(summary_lines) == 1, stderr
    # Every field should be n/a because we timed out before the WS probe
    # returned any measurements.
    assert "ws_cap=n/a" in summary_lines[0]


# ----- WS step-size bug regression ---------------------------------


def test_ws_probe_pool_smaller_than_step_size_does_not_report_zero():
    """With the original `range(step_size, n+1, step_size)` loop, a
    pool of 40 tickers against step_size=50 never entered the loop,
    so max_confirmed_tickers stayed at 0 even when every subscription
    succeeded. The fixed version always includes the full pool as a
    checkpoint, so a 40-ticker success reports max=40."""
    # We don't have a real WS feed in the sandbox, so drive the
    # RealProbeTransport's step-calculation via a fake feed instance.
    # The test replaces the feed-creation call with a stub that lets
    # us verify what checkpoints the probe computes.
    from kalshi_arb.probe.probe import RealProbeTransport

    class _FakeAsyncFeed:
        def __init__(self):
            self._handlers = {}
            self.subscribe_calls = []

        def on(self, event):
            def deco(fn):
                self._handlers[event] = fn
                return fn
            return deco

        def subscribe(self, channel, market_tickers):
            self.subscribe_calls.append((channel, list(market_tickers)))
            # Immediately "deliver" one message per ticker so the
            # probe's coverage check passes.
            handler = self._handlers.get("orderbook_delta")
            if handler:
                for t in market_tickers:
                    handler({"market_ticker": t})

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    fake_feed = _FakeAsyncFeed()

    class _FakeAsync:
        def feed(self):
            return fake_feed

    class _FakeRest:
        def async_underlying(self):
            return _FakeAsync()

    transport = RealProbeTransport(rest=_FakeRest())
    # 40-ticker pool, step_size=50 -> original bug would never loop.
    tickers = [f"KXBTC-{i:03d}" for i in range(40)]
    result = asyncio.run(transport.ws_subscription_cap(tickers, step_size=50))

    assert result["max_confirmed_tickers"] == 40, (
        f"40-ticker pool should report 40 confirmed, got "
        f"{result['max_confirmed_tickers']}. Step-size regression."
    )
    # Exactly one step recorded (the whole pool as a single checkpoint).
    assert len(result["steps"]) == 1
    assert result["steps"][0]["subscribed"] == 40
    assert result["failed_at_tickers"] is None


def test_ws_probe_exact_step_size_pool_reports_step_size():
    """Exactly step_size tickers -> single step of step_size,
    max_confirmed_tickers == step_size."""
    from kalshi_arb.probe.probe import RealProbeTransport

    class _FakeAsyncFeed:
        def __init__(self):
            self._handlers = {}

        def on(self, event):
            def deco(fn):
                self._handlers[event] = fn
                return fn
            return deco

        def subscribe(self, channel, market_tickers):
            h = self._handlers.get("orderbook_delta")
            if h:
                for t in market_tickers:
                    h({"market_ticker": t})

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _FakeAsync:
        def feed(self):
            return _FakeAsyncFeed()

    class _FakeRest:
        def async_underlying(self):
            return _FakeAsync()

    transport = RealProbeTransport(rest=_FakeRest())
    tickers = [f"KXBTC-{i:03d}" for i in range(50)]
    result = asyncio.run(transport.ws_subscription_cap(tickers, step_size=50))
    assert result["max_confirmed_tickers"] == 50


# ----- CLI subprocess: stderr carries the right lines --------------


def _base_env():
    env = os.environ.copy()
    env.pop("LIVE_TRADING", None)
    env.pop("PAPER_MODE", None)
    env.pop("KALSHI_USE_DEMO", None)
    env.pop("KALSHI_API_KEY_ID", None)
    env["KALSHI_PRIVATE_KEY_PATH"] = "/nonexistent-key"
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def test_cli_refusal_stderr_includes_actionable_reason():
    """Pre-network refusals should make the underlying problem
    obvious even without the summary line (refusals happen before
    any measurement)."""
    env = _base_env()
    proc = subprocess.run(
        [sys.executable, "-m", "kalshi_arb.cli", "probe", "--env", "prod"],
        env=env, cwd=str(REPO_ROOT),
        capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode != 0
    assert "KALSHI_USE_DEMO=false" in proc.stderr


def test_cli_invalid_env_exits_with_clear_reason():
    env = _base_env()
    proc = subprocess.run(
        [sys.executable, "-m", "kalshi_arb.cli", "probe", "--env", "bogus"],
        env=env, cwd=str(REPO_ROOT),
        capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode != 0
    assert "--env must be 'demo' or 'prod'" in proc.stderr
