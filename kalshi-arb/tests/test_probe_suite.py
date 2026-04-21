"""Integration tests for the full probe suite.

Exercises `kalshi_arb.probe.probe.run` end-to-end with a fake
ProbeTransport so we test orchestration (sequence of probes, timeout
enforcement, strict-mode pass/fail, yaml writing, all-or-nothing file
semantics) without needing network access or pykalshi.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml

from kalshi_arb.probe.analysis import PROBE_COID_PREFIX, probe_coid
from kalshi_arb.probe.probe import (
    ProbeFailure,
    RealProbeTransport,
    run,
)


@dataclass
class _FakeMarket:
    ticker: str
    volume_24h: float = 1000.0


@dataclass
class FakeProbeTransport:
    """Deterministic in-memory transport. Each method returns a pre-baked
    dict shaped like the real transport's output."""

    markets: list[_FakeMarket] = field(default_factory=list)
    ws_result: dict[str, Any] = field(
        default_factory=lambda: {
            "max_confirmed_tickers": 400,
            "failed_at_tickers": None,
            "failure_mode": None,
            "steps": [{"subscribed": 400, "receiving_messages": 300, "coverage_pct": 75.0}],
        }
    )
    rest_write_result: dict[str, Any] = field(
        default_factory=lambda: {
            "samples": 100,
            "successful": 95,
            "errors_summary": {"unique_errors": []},
            "p50_ms": 35.0,
            "p95_ms": 62.0,
            "p99_ms": 85.0,
            "max_ms": 99.0,
        }
    )
    rate_limit_result: dict[str, Any] = field(
        default_factory=lambda: {
            "endpoint": "/markets?limit=1",
            "rates_tested": [
                {"rps": 1, "ok": 3, "errors": 0},
                {"rps": 5, "ok": 15, "errors": 0},
                {"rps": 10, "ok": 30, "errors": 0},
                {"rps": 20, "ok": 12, "errors": 3},
            ],
            "limit_hit_at_rps": 20,
            "retry_after_sec": 1.0,
            "max_successful_rps": 10,
        }
    )
    e2e_result: dict[str, Any] = field(
        default_factory=lambda: {
            "events_seen": 25,
            "orders_fired": 12,
            "samples": 12,
            "p50_ms": 55.0,
            "p95_ms": 110.0,
            "p99_ms": 140.0,
            "max_ms": 155.0,
        }
    )

    # Observability for tests
    coid_tags_seen: list[str] = field(default_factory=list)

    async def list_open_markets(
        self, series_prefixes: tuple[str, ...] = (), limit: int = 1000
    ) -> list[_FakeMarket]:
        if not self.markets:
            # Default: 50 markets so WS cap probe has enough to ramp.
            return [
                _FakeMarket(ticker=f"KXBTC-{i:03d}", volume_24h=5000 - i * 10)
                for i in range(50)
            ]
        return list(self.markets)

    async def ws_subscription_cap(
        self, tickers: list[str], *, step_size: int = 50
    ) -> dict[str, Any]:
        await asyncio.sleep(0)
        return dict(self.ws_result)

    async def rest_write_latency(
        self, ticker: str, samples: int, *, coid_tag: str
    ) -> dict[str, Any]:
        self.coid_tags_seen.append(coid_tag)
        await asyncio.sleep(0)
        out = dict(self.rest_write_result)
        out.setdefault("samples", samples)
        return out

    async def rest_rate_limit(self) -> dict[str, Any]:
        await asyncio.sleep(0)
        return dict(self.rate_limit_result)

    async def end_to_end_loop(
        self, ticker: str, wait_sec: float, *, coid_tag: str
    ) -> dict[str, Any]:
        self.coid_tags_seen.append(coid_tag)
        await asyncio.sleep(0)
        return dict(self.e2e_result)


# ---- Happy-path prod run ------------------------------------------


def test_prod_run_happy_path_writes_yaml(tmp_path: Path):
    transport = FakeProbeTransport()
    out = tmp_path / "detected_limits.yaml"
    results = asyncio.run(
        run(
            env="prod",
            transport=transport,
            universe_categories=["crypto"],
            write_path=out,
            e2e_wait_sec=0.1,
            rest_write_samples=100,
        )
    )

    assert out.exists(), "detected_limits.yaml should be written on prod pass"
    body = yaml.safe_load(out.read_text())
    assert body["environment"] == "prod"
    assert body["ts_utc"]
    assert body["ws_subscription"]["max_confirmed_tickers"] == 400
    assert body["rest_write_latency_ms"]["p95_ms"] == 62.0
    assert body["rest_rate_limit"]["max_successful_rps"] == 10
    assert body["end_to_end_loop_ms"]["samples"] == 12
    # environment tag on every block
    for block in ("ws_subscription", "rest_write_latency_ms",
                  "rest_rate_limit", "end_to_end_loop_ms"):
        assert body[block].get("environment") == "prod"
    # results object matches file
    assert results.environment == "prod"


def test_prod_run_uses_probe_coid_tags(tmp_path: Path):
    transport = FakeProbeTransport()
    asyncio.run(
        run(
            env="prod", transport=transport,
            universe_categories=["crypto"],
            write_path=tmp_path / "out.yaml",
            e2e_wait_sec=0.1,
        )
    )
    # Both order-placing probes must receive a coid_tag.
    assert "write" in transport.coid_tags_seen
    assert "e2e" in transport.coid_tags_seen


# ---- Failing prod run: yaml NOT written ---------------------------


def test_prod_run_with_too_few_ws_tickers_fails_and_no_yaml(tmp_path: Path):
    transport = FakeProbeTransport(
        ws_result={
            "max_confirmed_tickers": 5,   # below PROD_MIN_WS_CONFIRMED_TICKERS (10)
            "failed_at_tickers": None,
            "failure_mode": None,
            "steps": [],
        }
    )
    out = tmp_path / "detected_limits.yaml"
    with pytest.raises(ProbeFailure, match="ws_subscription"):
        asyncio.run(
            run(
                env="prod", transport=transport,
                universe_categories=["crypto"],
                write_path=out,
                e2e_wait_sec=0.1,
            )
        )
    assert not out.exists(), (
        "detected_limits.yaml must NOT be written when strict validation fails"
    )


def test_prod_run_with_all_rest_errors_fails(tmp_path: Path):
    transport = FakeProbeTransport(
        rest_write_result={
            "samples": 100, "successful": 0,
            "note": "all requests failed",
        }
    )
    out = tmp_path / "detected_limits.yaml"
    with pytest.raises(ProbeFailure, match="rest_write_latency_ms"):
        asyncio.run(
            run(
                env="prod", transport=transport,
                universe_categories=["crypto"],
                write_path=out,
            )
        )
    assert not out.exists()


def test_prod_run_with_e2e_too_few_samples_fails(tmp_path: Path):
    transport = FakeProbeTransport(
        e2e_result={
            "events_seen": 100, "orders_fired": 2,
            "samples": 2, "p95_ms": 60.0,
        }
    )
    out = tmp_path / "detected_limits.yaml"
    with pytest.raises(ProbeFailure, match="end_to_end_loop_ms"):
        asyncio.run(
            run(env="prod", transport=transport,
                universe_categories=["crypto"],
                write_path=out)
        )
    assert not out.exists()


# ---- Timeout semantics ---------------------------------------------


def test_probe_suite_times_out_cleanly(tmp_path: Path):
    class _HangingTransport(FakeProbeTransport):
        async def rest_write_latency(self, ticker, samples, *, coid_tag):
            await asyncio.sleep(10.0)
            return {}

    transport = _HangingTransport()
    out = tmp_path / "detected_limits.yaml"
    with pytest.raises(ProbeFailure, match="timeout"):
        asyncio.run(
            run(env="prod", transport=transport,
                universe_categories=["crypto"],
                write_path=out,
                timeout_sec=0.5,
                e2e_wait_sec=0.1)
        )
    assert not out.exists()


# ---- Probe raising propagates to ProbeFailure ----------------------


def test_unexpected_fill_raises_probe_failure(tmp_path: Path):
    """A transport that internally calls ProbeFailure (mirroring the
    CRITICAL unfillable-fill branch) must prevent any yaml write."""
    class _FillTransport(FakeProbeTransport):
        async def rest_write_latency(self, ticker, samples, *, coid_tag):
            raise ProbeFailure(
                "rest_write_latency: 1 unfillable orders filled on KXBTC-000"
            )

    transport = _FillTransport()
    out = tmp_path / "detected_limits.yaml"
    with pytest.raises(ProbeFailure, match="unfillable"):
        asyncio.run(
            run(env="prod", transport=transport,
                universe_categories=["crypto"],
                write_path=out)
        )
    assert not out.exists()


# ---- Demo happy path -----------------------------------------------


def test_demo_run_skips_e2e_and_writes_yaml(tmp_path: Path):
    transport = FakeProbeTransport()
    out = tmp_path / "detected_limits.yaml"
    results = asyncio.run(
        run(env="demo", transport=transport,
            universe_categories=["crypto"],
            write_path=out)
    )
    assert out.exists()
    body = yaml.safe_load(out.read_text())
    assert body["environment"] == "demo"
    # E2E deferred in demo mode regardless of strict thresholds.
    assert body["end_to_end_loop_ms"].get("status") == "deferred"
    assert results.environment == "demo"


def test_demo_run_writes_yaml_even_if_strict_thresholds_would_fail(tmp_path: Path):
    """Demo numbers are informational; low ws-cap should not block
    the yaml write."""
    transport = FakeProbeTransport(
        ws_result={
            "max_confirmed_tickers": 5,
            "failed_at_tickers": None, "failure_mode": None, "steps": [],
        }
    )
    out = tmp_path / "detected_limits.yaml"
    asyncio.run(
        run(env="demo", transport=transport,
            universe_categories=["crypto"],
            write_path=out)
    )
    assert out.exists()


# ---- Write gating --------------------------------------------------


def test_write_enabled_false_returns_results_but_writes_nothing(tmp_path: Path):
    transport = FakeProbeTransport()
    out = tmp_path / "detected_limits.yaml"
    results = asyncio.run(
        run(env="prod", transport=transport,
            universe_categories=["crypto"],
            write_path=out,
            write_enabled=False,
            e2e_wait_sec=0.1)
    )
    assert results.environment == "prod"
    assert not out.exists()


# ---- RealProbeTransport surface check ------------------------------


def test_real_probe_transport_has_all_methods():
    """Covers the `Protocol` surface without instantiating pykalshi:
    makes sure refactors don't silently drop a method the orchestrator
    calls."""
    for name in (
        "list_open_markets",
        "ws_subscription_cap",
        "rest_write_latency",
        "rest_rate_limit",
        "end_to_end_loop",
    ):
        assert hasattr(RealProbeTransport, name), (
            f"RealProbeTransport missing required method {name}"
        )


# ---- probe_coid helper sanity -------------------------------------


def test_probe_coid_roundtrip():
    c = probe_coid(123456789, 7, tag="write")
    assert c.startswith(PROBE_COID_PREFIX)
    assert "write-123456789-7" in c
