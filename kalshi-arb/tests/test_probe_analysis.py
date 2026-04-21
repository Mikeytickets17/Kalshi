"""Unit tests for probe/analysis.py pure functions.

Every test takes hand-constructed fixture data and asserts the exact
output, so a regression in percentile math, rate-limit summarisation,
yaml shape, or pass/fail thresholds shows up as a failed test.
"""

from __future__ import annotations

from kalshi_arb.probe.analysis import (
    PROBE_COID_PREFIX,
    PROD_MIN_E2E_SAMPLES,
    PROD_MIN_RATE_RPS,
    PROD_MIN_WRITE_SUCCESS_PCT,
    PROD_MIN_WS_CONFIRMED_TICKERS,
    ProbeResults,
    build_yaml_body,
    is_probe_coid,
    percentiles,
    probe_coid,
    rate_limit_summary,
    validate_prod_results,
)


# ---- percentiles ---------------------------------------------------


def test_percentiles_empty_input_returns_empty_dict():
    assert percentiles([]) == {}


def test_percentiles_single_value_returns_same_for_every_pct():
    p = percentiles([50.0])
    assert p["samples"] == 1
    assert p["p50_ms"] == 50.0
    assert p["p95_ms"] == 50.0
    assert p["p99_ms"] == 50.0
    assert p["max_ms"] == 50.0


def test_percentiles_100_samples_uniform_1_to_100():
    values = [float(i) for i in range(1, 101)]
    p = percentiles(values)
    assert p["samples"] == 100
    assert p["p50_ms"] == 50.5   # median of 1..100
    assert p["p95_ms"] == 95.0
    assert p["p99_ms"] == 99.0
    assert p["max_ms"] == 100.0


def test_percentiles_sorts_unsorted_input():
    p = percentiles([3.0, 1.0, 2.0])
    assert p["p50_ms"] == 2.0
    assert p["max_ms"] == 3.0


# ---- rate_limit_summary --------------------------------------------


def test_rate_limit_summary_empty():
    r = rate_limit_summary([])
    assert r["max_successful_rps"] == 0
    assert r["limit_hit_at_rps"] is None


def test_rate_limit_summary_429_at_20rps():
    """1/2/5/10 all clean; 20 hits 429. Summary should report 10 as
    max_successful and 20 as the ceiling."""
    rates = [
        {"rps": 1, "ok": 3, "errors": 0},
        {"rps": 2, "ok": 6, "errors": 0},
        {"rps": 5, "ok": 15, "errors": 0},
        {"rps": 10, "ok": 30, "errors": 0},
        {"rps": 20, "ok": 12, "errors": 3},
    ]
    r = rate_limit_summary(rates)
    assert r["max_successful_rps"] == 10
    assert r["limit_hit_at_rps"] == 20


def test_rate_limit_summary_no_429_reports_highest_clean_rps():
    rates = [
        {"rps": 1, "ok": 3, "errors": 0},
        {"rps": 10, "ok": 30, "errors": 0},
        {"rps": 40, "ok": 120, "errors": 0},
    ]
    r = rate_limit_summary(rates)
    assert r["max_successful_rps"] == 40
    assert r["limit_hit_at_rps"] is None


# ---- build_yaml_body -----------------------------------------------


def test_build_yaml_body_shape():
    r = ProbeResults(
        ts_utc="2026-04-20T16:00:00Z",
        environment="prod",
        ws_subscription={"max_confirmed_tickers": 400},
        rest_write_latency_ms={"p95_ms": 42.0, "samples": 100, "successful": 95},
        rest_rate_limit={"max_successful_rps": 10, "limit_hit_at_rps": 20},
        end_to_end_loop_ms={"p95_ms": 85.0, "samples": 12},
        notes=["prod"],
    )
    body = build_yaml_body(r)
    assert body["environment"] == "prod"
    assert body["ts_utc"] == "2026-04-20T16:00:00Z"
    assert body["ws_subscription"]["max_confirmed_tickers"] == 400
    assert body["rest_write_latency_ms"]["p95_ms"] == 42.0
    assert body["rest_rate_limit"]["limit_hit_at_rps"] == 20
    assert body["end_to_end_loop_ms"]["p95_ms"] == 85.0
    assert body["notes"] == ["prod"]
    # Fixed key order so yaml diffs are reviewable.
    keys = list(body.keys())
    assert keys == [
        "ts_utc", "environment",
        "ws_subscription", "rest_write_latency_ms",
        "rest_rate_limit", "end_to_end_loop_ms", "notes",
    ]


# ---- validate_prod_results -----------------------------------------


def _healthy_results() -> ProbeResults:
    return ProbeResults(
        ts_utc="2026-04-20T16:00:00Z",
        environment="prod",
        ws_subscription={"max_confirmed_tickers": 400},
        rest_write_latency_ms={
            "samples": 100, "successful": 95,
            "p50_ms": 35.0, "p95_ms": 60.0, "p99_ms": 80.0, "max_ms": 95.0,
        },
        rest_rate_limit={"max_successful_rps": 20, "limit_hit_at_rps": 40},
        end_to_end_loop_ms={
            "samples": 15, "events_seen": 40,
            "orders_fired": 15, "p50_ms": 60.0, "p95_ms": 120.0,
        },
    )


def test_validate_prod_results_healthy_passes():
    assert validate_prod_results(_healthy_results()) == []


def test_validate_prod_results_low_ws_tickers_fails():
    r = _healthy_results()
    r.ws_subscription["max_confirmed_tickers"] = PROD_MIN_WS_CONFIRMED_TICKERS - 1
    fails = validate_prod_results(r)
    assert len(fails) == 1
    assert "ws_subscription" in fails[0]


def test_validate_prod_results_all_rest_errors_fails():
    r = _healthy_results()
    r.rest_write_latency_ms = {
        "samples": 100, "successful": 0,
        "note": "all failed",
    }
    fails = validate_prod_results(r)
    assert any("rest_write_latency_ms" in f for f in fails)
    assert any("success_rate=0.0%" in f for f in fails)


def test_validate_prod_results_low_rest_write_success_fails():
    r = _healthy_results()
    r.rest_write_latency_ms = {
        "samples": 100, "successful": 70,   # 70% < 80% threshold
        "p50_ms": 35.0, "p95_ms": 60.0, "p99_ms": 80.0, "max_ms": 95.0,
    }
    fails = validate_prod_results(r)
    assert any("success_rate=70.0%" in f for f in fails)


def test_validate_prod_results_missing_p95_fails():
    r = _healthy_results()
    r.rest_write_latency_ms.pop("p95_ms")
    fails = validate_prod_results(r)
    assert any("no p95 produced" in f for f in fails)


def test_validate_prod_results_rate_limit_too_low_fails():
    r = _healthy_results()
    r.rest_rate_limit["max_successful_rps"] = PROD_MIN_RATE_RPS - 1
    fails = validate_prod_results(r)
    assert any("rest_rate_limit" in f for f in fails)


def test_validate_prod_results_e2e_too_few_samples_fails():
    r = _healthy_results()
    r.end_to_end_loop_ms["samples"] = PROD_MIN_E2E_SAMPLES - 1
    fails = validate_prod_results(r)
    assert any("end_to_end_loop_ms" in f for f in fails)


def test_validate_prod_results_lists_all_failures_not_just_first():
    r = _healthy_results()
    r.ws_subscription["max_confirmed_tickers"] = 0
    r.rest_rate_limit["max_successful_rps"] = 0
    r.end_to_end_loop_ms["samples"] = 0
    fails = validate_prod_results(r)
    assert len(fails) >= 3


# ---- probe COID helpers -------------------------------------------


def test_probe_coid_shape():
    c = probe_coid(1700000000000, 5, tag="write")
    assert c.startswith(PROBE_COID_PREFIX)
    assert "write" in c
    assert "1700000000000" in c
    assert c.endswith("-5")


def test_probe_coid_is_probe_recognized():
    c = probe_coid(1, 2, tag="e2e")
    assert is_probe_coid(c) is True
    assert is_probe_coid("paper-execution-123") is False
    assert is_probe_coid(None) is False


# ---- threshold constants are documented ---------------------------


def test_threshold_constants_are_sane():
    """If someone tightens these, this test catches the intent so
    downstream ProbeFailure messages still make sense."""
    assert 50.0 <= PROD_MIN_WRITE_SUCCESS_PCT <= 100.0
    assert 1 <= PROD_MIN_WS_CONFIRMED_TICKERS <= 1000
    assert 1 <= PROD_MIN_RATE_RPS <= 100
    assert 1 <= PROD_MIN_E2E_SAMPLES <= 100
