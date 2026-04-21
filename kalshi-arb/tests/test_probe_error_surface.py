"""Tests for surfacing Kalshi's actual error responses into the popup.

The operator ran the prod probe and saw success_rate=0.0% on REST
write + samples=0 on E2E. Popup said "threshold not met" but never
revealed the HTTP status or Kalshi error body, so they couldn't tell
whether it was auth, insufficient-buying-power, validation, or a
tier restriction. These tests pin the contract that answers that:

  * parse_kalshi_error_body -> 'code: message' from Kalshi JSON
  * summarize_error_groups  -> one popup-line per unique (status, body)
    group, sorted by count descending
  * build_error_detail_lines -> full result shape to popup-bullets
  * end-to-end: a realistic-prod transport whose create_order raises
    httpx.HTTPStatusError with Kalshi JSON body produces the expected
    "REST WRITE 100x HTTP 403 \"insufficient_buying_power: ...\""
    on stderr during ProbeFailure emission
"""

from __future__ import annotations

import asyncio
import io
import json
from contextlib import redirect_stderr
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from kalshi_arb.common.errors import ErrorCapture
from kalshi_arb.probe.analysis import (
    FAIL_DETAIL_PREFIX,
    ProbeResults,
    build_error_detail_lines,
    parse_kalshi_error_body,
    summarize_error_groups,
)
from kalshi_arb.probe.probe import ProbeFailure, run


# ---- parse_kalshi_error_body ---------------------------------------


def test_parse_kalshi_error_body_nested_under_error_key():
    body = json.dumps({
        "error": {
            "code": "insufficient_buying_power",
            "message": "balance=$27, required=$0.50",
        }
    })
    assert parse_kalshi_error_body(body) == (
        "insufficient_buying_power: balance=$27, required=$0.50"
    )


def test_parse_kalshi_error_body_top_level_code_message():
    """Some Kalshi endpoints return flat shape {code, message}."""
    body = json.dumps({"code": "invalid_signature", "message": "bad sig"})
    assert parse_kalshi_error_body(body) == "invalid_signature: bad sig"


def test_parse_kalshi_error_body_only_code_no_message():
    body = json.dumps({"error": {"code": "forbidden"}})
    assert parse_kalshi_error_body(body) == "forbidden"


def test_parse_kalshi_error_body_only_message_no_code():
    body = json.dumps({"error": {"message": "rate limit exceeded"}})
    assert parse_kalshi_error_body(body) == "rate limit exceeded"


def test_parse_kalshi_error_body_bytes_input():
    body = json.dumps({"error": {"code": "x", "message": "y"}}).encode()
    assert parse_kalshi_error_body(body) == "x: y"


def test_parse_kalshi_error_body_non_json_returns_none():
    assert parse_kalshi_error_body("<html>500 Internal Server Error</html>") is None
    assert parse_kalshi_error_body(None) is None


def test_parse_kalshi_error_body_json_without_error_fields_returns_none():
    assert parse_kalshi_error_body(json.dumps({"foo": "bar"})) is None


def test_parse_kalshi_error_body_uses_kind_and_detail_as_fallback():
    """Some OpenAPI shapes use 'kind'/'detail' instead of 'code'/'message'."""
    body = json.dumps({"error": {"kind": "forbidden", "detail": "allow list"}})
    assert parse_kalshi_error_body(body) == "forbidden: allow list"


# ---- summarize_error_groups ----------------------------------------


def _capture_from(samples: list[dict[str, Any]]) -> dict[str, Any]:
    """Build an ErrorCapture-shaped dict from raw sample records."""
    return {
        "total_calls": sum(s.get("count", 1) for s in samples),
        "total_errors": sum(s.get("count", 1) for s in samples),
        "error_rate": 1.0 if samples else 0.0,
        "unique_errors_captured": len(samples),
        "samples": samples,
    }


def test_summarize_error_groups_empty_input():
    assert summarize_error_groups(None, label="REST WRITE") == []
    assert summarize_error_groups({}, label="REST WRITE") == []
    assert summarize_error_groups(_capture_from([]), label="REST WRITE") == []


def test_summarize_error_groups_single_403_body():
    cap = _capture_from([{
        "error_class": "HTTPStatusError",
        "http_status": 403,
        "message": "Client error '403 Forbidden'",
        "body_excerpt": json.dumps({
            "error": {
                "code": "insufficient_buying_power",
                "message": "balance=$27, required=$0.50",
            }
        }),
        "count": 100,
    }])
    lines = summarize_error_groups(cap, label="REST WRITE")
    assert len(lines) == 1
    assert lines[0] == (
        'REST WRITE 100x HTTP 403 '
        '"insufficient_buying_power: balance=$27, required=$0.50"'
    )


def test_summarize_error_groups_multiple_sorted_by_count():
    """Mixed 401 + 400 + 403; output must lead with the most-common."""
    samples = [
        {
            "error_class": "HTTPStatusError", "http_status": 400,
            "message": "", "count": 30,
            "body_excerpt": json.dumps({"error": {
                "code": "order_validation_failed",
                "message": "min_quantity=1",
            }}),
        },
        {
            "error_class": "HTTPStatusError", "http_status": 401,
            "message": "", "count": 50,
            "body_excerpt": json.dumps({"error": {
                "code": "invalid_signature", "message": "bad sig",
            }}),
        },
        {
            "error_class": "HTTPStatusError", "http_status": 403,
            "message": "", "count": 20,
            "body_excerpt": json.dumps({"error": {
                "code": "forbidden", "message": "",
            }}),
        },
    ]
    lines = summarize_error_groups(_capture_from(samples), label="REST WRITE")
    assert len(lines) == 3
    # Sorted by count descending: 50 > 30 > 20.
    assert lines[0].startswith("REST WRITE 50x HTTP 401")
    assert "invalid_signature: bad sig" in lines[0]
    assert lines[1].startswith("REST WRITE 30x HTTP 400")
    assert "order_validation_failed: min_quantity=1" in lines[1]
    assert lines[2].startswith("REST WRITE 20x HTTP 403")
    assert "forbidden" in lines[2]


def test_summarize_error_groups_falls_back_to_raw_body_on_non_json():
    cap = _capture_from([{
        "error_class": "ConnectionError",
        "http_status": None,
        "message": "connection reset",
        "body_excerpt": "connection reset by peer",
        "count": 5,
    }])
    lines = summarize_error_groups(cap, label="E2E LOOP")
    assert len(lines) == 1
    assert lines[0].startswith("E2E LOOP 5x ConnectionError")
    assert "connection reset by peer" in lines[0]


def test_summarize_error_groups_truncates_long_bodies():
    """A 10kb error body should not break the single-line popup layout."""
    long_body = "a" * 1000
    cap = _capture_from([{
        "error_class": "HTTPStatusError", "http_status": 500,
        "message": "", "body_excerpt": long_body, "count": 1,
    }])
    lines = summarize_error_groups(cap, label="REST WRITE")
    assert len(lines) == 1
    assert len(lines[0]) < 400, (
        f"popup line should be short; got len={len(lines[0])}"
    )


# ---- build_error_detail_lines (full-result wiring) -----------------


def test_build_error_detail_lines_emits_header_per_block_with_errors():
    r = ProbeResults(environment="prod")
    r.rest_write_latency_ms = {
        "samples": 100, "successful": 0,
        "errors_summary": _capture_from([{
            "error_class": "HTTPStatusError", "http_status": 403,
            "message": "", "count": 100,
            "body_excerpt": json.dumps({"error": {
                "code": "insufficient_buying_power",
                "message": "balance=$27",
            }}),
        }]),
    }
    r.end_to_end_loop_ms = {
        "events_seen": 30, "orders_fired": 0, "samples": 0,
        "errors_summary": _capture_from([{
            "error_class": "HTTPStatusError", "http_status": 403,
            "message": "", "count": 30,
            "body_excerpt": json.dumps({"error": {
                "code": "insufficient_buying_power",
                "message": "balance=$27",
            }}),
        }]),
    }

    lines = build_error_detail_lines(r)
    # Expect header + one group line per block that had errors.
    joined = "\n".join(lines)
    assert "REST WRITE FAILURES: 100/100 failed" in joined
    assert "REST WRITE 100x HTTP 403" in joined
    assert "insufficient_buying_power: balance=$27" in joined
    assert "E2E LOOP FAILURES: 30/30 failed" in joined
    assert "E2E LOOP 30x HTTP 403" in joined


def test_build_error_detail_lines_skips_blocks_with_no_errors():
    r = ProbeResults(environment="prod")
    r.rest_write_latency_ms = {
        "samples": 100, "successful": 100,
        "errors_summary": _capture_from([]),  # zero errors
    }
    # Missing errors_summary should also not produce a header.
    r.end_to_end_loop_ms = {"events_seen": 10, "orders_fired": 10, "samples": 10}
    assert build_error_detail_lines(r) == []


def test_build_error_detail_lines_returns_empty_for_empty_results():
    r = ProbeResults(environment="prod")
    assert build_error_detail_lines(r) == []


# ---- End-to-end: popup stderr sees grouped errors on failure ------


@dataclass
class _Market:
    ticker: str
    volume_24h: float = 1000.0


@dataclass
class _Kalshi403Transport:
    """Simulates prod: REST write + E2E both reject every order with
    HTTP 403 and a JSON body reporting insufficient_buying_power.

    This matches the shape the operator hit: 100/100 REST write
    failures, samples=0 E2E, no auth errors (HTTP 403 is NOT an auth
    problem), rate-limit probe succeeds (GET /markets doesn't place
    orders), WS subscription fine (20 tickers)."""

    failures_per_probe: int = 100

    async def list_open_markets(self, series_prefixes=(), limit=1000):
        return [_Market(ticker=f"KXBTC-PROD-{i}") for i in range(20)]

    async def ws_subscription_cap(self, tickers, *, step_size=50):
        return {"max_confirmed_tickers": 20}

    async def rest_write_latency(self, ticker, samples, *, coid_tag):
        # Simulate a real ErrorCapture from a 100/100 failure run.
        cap = ErrorCapture(max_unique=5)
        body = json.dumps({"error": {
            "code": "insufficient_buying_power",
            "message": "balance=$27, required=$0.50",
        }})
        for i in range(self.failures_per_probe):
            cap.record(
                _build_http_error(403, body),
                context={"iter": i, "ticker": ticker},
            )
        return {
            "samples": samples,
            "successful": 0,
            "errors_summary": cap.to_dict(),
            "note": "all requests failed",
        }

    async def rest_rate_limit(self):
        return {"max_successful_rps": 10, "rates_tested": [
            {"rps": 10, "ok": 30, "errors": 0},
        ]}

    async def end_to_end_loop(self, ticker, wait_sec, *, coid_tag):
        cap = ErrorCapture(max_unique=5)
        body = json.dumps({"error": {
            "code": "insufficient_buying_power",
            "message": "balance=$27, required=$0.50",
        }})
        # E2E saw 30 events, tried to fire 30 orders, all rejected.
        for i in range(30):
            cap.record(
                _build_http_error(403, body),
                context={"iter": i, "ticker": ticker},
            )
        return {
            "events_seen": 30,
            "orders_fired": 0,
            "samples": 0,
            "errors_summary": cap.to_dict(),
        }


def _build_http_error(status: int, body: str) -> Exception:
    """Produce an exception whose .response.status_code + .response.text
    match httpx.HTTPStatusError, so ErrorCapture's extractors work."""
    @dataclass
    class _FakeResponse:
        status_code: int
        text: str

    class _HTTPError(Exception):
        def __init__(self, status: int, text: str) -> None:
            super().__init__(f"{status} error")
            self.response = _FakeResponse(status_code=status, text=text)

    return _HTTPError(status, body)


def test_end_to_end_failure_surfaces_kalshi_error_bodies_to_stderr(tmp_path):
    """The scenario the operator hit: prod, REST 100/100, E2E 30/30.
    Stderr must carry SUMMARY + threshold detail + grouped Kalshi
    error bodies, so the popup bullet list answers 'WHY rejected?'.
    """
    out = tmp_path / "detected_limits.yaml"
    buf = io.StringIO()
    with redirect_stderr(buf):
        with pytest.raises(ProbeFailure):
            asyncio.run(
                run(env="prod",
                    transport=_Kalshi403Transport(),
                    universe_categories=["crypto"],
                    write_path=out,
                    e2e_wait_sec=0.1)
            )
    stderr = buf.getvalue()

    # yaml must NOT be written (all-or-nothing invariant preserved).
    assert not out.exists()

    details = [l for l in stderr.splitlines() if l.startswith(FAIL_DETAIL_PREFIX)]
    joined = "\n".join(details)

    # Threshold-miss headers still land.
    assert any("rest_write_latency_ms: success_rate=0.0%" in l for l in details)
    assert any("end_to_end_loop_ms: samples=0" in l for l in details)

    # Grouped error headers tell the operator the failure ratio.
    assert "REST WRITE FAILURES: 100/100 failed" in joined
    assert "E2E LOOP FAILURES: 30/30 failed" in joined

    # AND the actual Kalshi response body is in there, the operator's
    # north-star deliverable: they can SEE why Kalshi rejected.
    assert "REST WRITE 100x HTTP 403" in joined
    assert "insufficient_buying_power: balance=$27, required=$0.50" in joined
    assert "E2E LOOP 30x HTTP 403" in joined


def test_end_to_end_failure_with_multiple_error_groups_sorts_by_count(tmp_path):
    """Operator's hypothetical mixed failure: 50x 401 + 50x 400. The
    popup line order must reflect frequency so the most common failure
    leads the bullets."""

    @dataclass
    class _MixedTransport(_Kalshi403Transport):
        async def rest_write_latency(self, ticker, samples, *, coid_tag):
            cap = ErrorCapture(max_unique=5)
            body_401 = json.dumps({"error": {
                "code": "invalid_signature", "message": "bad sig",
            }})
            body_400 = json.dumps({"error": {
                "code": "order_validation_failed", "message": "min_quantity=1",
            }})
            for i in range(50):
                cap.record(
                    _build_http_error(401, body_401),
                    context={"iter": i},
                )
            for i in range(50):
                cap.record(
                    _build_http_error(400, body_400),
                    context={"iter": i + 50},
                )
            return {
                "samples": samples, "successful": 0,
                "errors_summary": cap.to_dict(),
            }

    out = tmp_path / "detected_limits.yaml"
    buf = io.StringIO()
    with redirect_stderr(buf):
        with pytest.raises(ProbeFailure):
            asyncio.run(
                run(env="prod", transport=_MixedTransport(),
                    universe_categories=["crypto"],
                    write_path=out, e2e_wait_sec=0.1)
            )
    stderr = buf.getvalue()

    rest_lines = [
        l for l in stderr.splitlines()
        if l.startswith(FAIL_DETAIL_PREFIX) and "REST WRITE " in l
        and "FAILURES" not in l   # skip the header line
    ]
    # Both groups present; tie-broken stably so both orderings allowed
    # but each group must carry its own count + code.
    assert len(rest_lines) == 2
    line_401 = next((l for l in rest_lines if "HTTP 401" in l), None)
    line_400 = next((l for l in rest_lines if "HTTP 400" in l), None)
    assert line_401 and "50x HTTP 401" in line_401
    assert line_401 and "invalid_signature: bad sig" in line_401
    assert line_400 and "50x HTTP 400" in line_400
    assert line_400 and "order_validation_failed: min_quantity=1" in line_400


def test_success_run_does_not_emit_error_detail_lines(tmp_path):
    """Healthy run: no FAILED DETAIL lines should leak even though
    build_error_detail_lines is called on every block. (Actually it's
    called only on failure now -- pin that invariant.)"""

    @dataclass
    class _HealthyTransport(_Kalshi403Transport):
        async def rest_write_latency(self, ticker, samples, *, coid_tag):
            return {
                "samples": 100, "successful": 100,
                "p50_ms": 35.0, "p95_ms": 60.0, "p99_ms": 80.0, "max_ms": 95.0,
                "errors_summary": _capture_from([]),
            }

        async def end_to_end_loop(self, ticker, wait_sec, *, coid_tag):
            return {
                "events_seen": 10, "orders_fired": 10, "samples": 10,
                "p50_ms": 50.0, "p95_ms": 80.0,
                "errors_summary": _capture_from([]),
            }

    out = tmp_path / "detected_limits.yaml"
    buf = io.StringIO()
    with redirect_stderr(buf):
        asyncio.run(
            run(env="prod", transport=_HealthyTransport(),
                universe_categories=["crypto"],
                write_path=out, e2e_wait_sec=0.1)
        )
    stderr = buf.getvalue()
    # Run passed -> no PROBE FAILED DETAIL anywhere.
    assert FAIL_DETAIL_PREFIX not in stderr
    assert out.exists()
