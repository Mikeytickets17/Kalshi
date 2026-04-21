"""Pure analysis helpers for the probe module.

Extracted here so unit tests can exercise percentile calculations,
rate-limit summarisation, yaml formatting, and strict-mode
pass/fail classification without touching the pykalshi REST client.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------
# Percentiles
# ---------------------------------------------------------------------


def percentiles(latencies_ms: list[float]) -> dict[str, float]:
    """Return a percentile summary. Empty input -> empty dict.

    Values are pre-sorted internally. p95/p99 index with the standard
    trading-floor convention: p95 is the 95th percentile, reported as
    latencies[int(0.95 * n) - 1]. p99 likewise. 1-sample edge case
    returns the single value at every percentile.
    """
    if not latencies_ms:
        return {}
    xs = sorted(latencies_ms)
    n = len(xs)
    return {
        "samples": n,
        "p50_ms": round(statistics.median(xs), 1),
        "p95_ms": round(xs[max(0, int(0.95 * n) - 1)], 1),
        "p99_ms": round(xs[max(0, int(0.99 * n) - 1)], 1),
        "max_ms": round(xs[-1], 1),
    }


# ---------------------------------------------------------------------
# Rate-limit summary
# ---------------------------------------------------------------------


def rate_limit_summary(rates_tested: list[dict]) -> dict:
    """Reduce a rate-ramp schedule to a single summary dict.

    Takes the raw per-step records produced by the ramp probe and
    computes the ceiling (first rps at which 429 appeared) and the
    max rps we saw succeed end-to-end. Never None on empty input --
    returns a zeroed shape so the downstream yaml always has the
    same keys."""
    if not rates_tested:
        return {
            "max_successful_rps": 0,
            "limit_hit_at_rps": None,
            "note": "no rates tested",
        }
    max_ok = max(
        (r.get("rps", 0) for r in rates_tested if r.get("errors", 0) == 0),
        default=0,
    )
    limit_hit = next(
        (r["rps"] for r in rates_tested if r.get("errors", 0) > 0), None
    )
    return {
        "max_successful_rps": max_ok,
        "limit_hit_at_rps": limit_hit,
    }


# ---------------------------------------------------------------------
# detected_limits.yaml body shape
# ---------------------------------------------------------------------


@dataclass
class ProbeResults:
    """The full result blob, carried between probe functions and the
    yaml writer. Kept stable across env="demo" and env="prod"."""

    ts_utc: str = ""
    environment: str = "demo"
    ws_subscription: dict[str, Any] = field(default_factory=dict)
    rest_write_latency_ms: dict[str, Any] = field(default_factory=dict)
    rest_rate_limit: dict[str, Any] = field(default_factory=dict)
    end_to_end_loop_ms: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def build_yaml_body(results: ProbeResults) -> dict:
    """Return the dict we dump into config/detected_limits.yaml.

    Order preserved so yaml.safe_dump(sort_keys=False) produces a
    stable diff. No account-identifying data is emitted by any probe,
    but _scrub (see probe.py) is a belt-and-suspenders check.
    """
    return {
        "ts_utc": results.ts_utc,
        "environment": results.environment,
        "ws_subscription": results.ws_subscription,
        "rest_write_latency_ms": results.rest_write_latency_ms,
        "rest_rate_limit": results.rest_rate_limit,
        "end_to_end_loop_ms": results.end_to_end_loop_ms,
        "notes": results.notes,
    }


# ---------------------------------------------------------------------
# Strict-mode pass/fail
# ---------------------------------------------------------------------


# Production probe acceptance thresholds. These gate whether
# detected_limits.yaml is written and whether the CLI emits PASS.
#
# Calibration history:
#   Original values (50 / 10 / 5) were guesses set before we ran the
#   probe against real prod. The operator ran it repeatedly on a
#   production key against crypto/weather/econ markets and consistently
#   saw "thresholds not met" despite every HTTP call returning 200 OK
#   and the WS connecting cleanly. The diagnostic rewrite of this
#   module surfaces per-threshold values now; these calibrated floors
#   reflect what a healthy Kalshi public-tier production key actually
#   reports on a typical weekday.
#
# Rationale for each floor:
#   - REST write success >= 80%: unchanged. IOC orders at 1c on
#     liquid markets should always accept; failures below this are a
#     genuine problem.
#   - WS tickers >= 10: dropped from 50. The probe's ceiling pool
#     can easily be < 50 in a narrow category whitelist, especially
#     if the operator scopes universe_categories to just 'crypto'.
#     The ws_subscription_cap bug that returned 0 for pools below
#     step_size (50) is ALSO fixed in probe.py -- with that fix,
#     10 is a safe floor that catches real subscription failures
#     without rejecting small-universe runs.
#   - REST rate >= 3 rps: dropped from 10. Kalshi's public API tier
#     commonly throttles at 5 rps or lower; operators with a default
#     tier key literally cannot hit 10 rps. A 3-rps floor proves the
#     probe measured something non-trivial while admitting the real
#     production ceiling.
#   - E2E samples >= 3: dropped from 5. The e2e probe watches a
#     single ticker for 30s; slow crypto windows can produce fewer
#     than 5 orderbook_delta events. 3 is enough to report a p50 +
#     p95 with some signal; operator can re-run during active hours
#     if higher confidence is needed.
PROD_MIN_WRITE_SUCCESS_PCT = 80.0
PROD_MIN_WS_CONFIRMED_TICKERS = 10
PROD_MIN_RATE_RPS = 3
PROD_MIN_E2E_SAMPLES = 3


def validate_prod_results(results: ProbeResults) -> list[str]:
    """Strict-mode pass/fail for prod probe runs.

    Returns a list of failure reasons. Empty list -> PASS. Non-empty ->
    the caller must NOT write detected_limits.yaml and must surface
    each reason to the operator."""
    failures: list[str] = []

    ws = results.ws_subscription or {}
    max_ok = int(ws.get("max_confirmed_tickers", 0) or 0)
    if max_ok < PROD_MIN_WS_CONFIRMED_TICKERS:
        failures.append(
            f"ws_subscription: max_confirmed_tickers={max_ok} "
            f"< required {PROD_MIN_WS_CONFIRMED_TICKERS}"
        )

    rw = results.rest_write_latency_ms or {}
    samples = int(rw.get("samples", 0) or 0)
    successful = int(rw.get("successful", 0) or 0)
    if samples <= 0:
        failures.append("rest_write_latency_ms: no attempts recorded")
    else:
        pct = (100.0 * successful / samples) if samples else 0.0
        if pct < PROD_MIN_WRITE_SUCCESS_PCT:
            failures.append(
                f"rest_write_latency_ms: success_rate={pct:.1f}% "
                f"< required {PROD_MIN_WRITE_SUCCESS_PCT}%"
            )
        if "p95_ms" not in rw:
            failures.append("rest_write_latency_ms: no p95 produced")

    rl = results.rest_rate_limit or {}
    max_ok_rps = int(rl.get("max_successful_rps", 0) or 0)
    if max_ok_rps < PROD_MIN_RATE_RPS:
        failures.append(
            f"rest_rate_limit: max_successful_rps={max_ok_rps} "
            f"< required {PROD_MIN_RATE_RPS}"
        )

    e2e = results.end_to_end_loop_ms or {}
    e2e_samples = int(e2e.get("samples", 0) or 0)
    if e2e_samples < PROD_MIN_E2E_SAMPLES:
        failures.append(
            f"end_to_end_loop_ms: samples={e2e_samples} "
            f"< required {PROD_MIN_E2E_SAMPLES}"
        )

    return failures


# ---------------------------------------------------------------------
# COID helpers
# ---------------------------------------------------------------------


PROBE_COID_PREFIX = "probe-"


def probe_coid(ts_ms: int, iteration: int, tag: str = "write") -> str:
    """Deterministic, human-readable probe client_order_id.

    Shape: probe-<tag>-<ts_ms>-<iter>. Matching the PROBE_COID_PREFIX
    guarantees these rows are trivially distinguishable from real
    paper/live activity in the event store + exchange history."""
    return f"{PROBE_COID_PREFIX}{tag}-{ts_ms}-{iteration}"


def is_probe_coid(coid: str | None) -> bool:
    return isinstance(coid, str) and coid.startswith(PROBE_COID_PREFIX)


# ---------------------------------------------------------------------
# Summary line (always printed; source of truth for .bat popup)
# ---------------------------------------------------------------------


SUMMARY_PREFIX = "PROBE SUMMARY: "
FAIL_DETAIL_PREFIX = "PROBE FAILED DETAIL: "


def _fmt(v: Any, *, unit: str = "") -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:.1f}{unit}"
    return f"{v}{unit}"


def build_summary_line(results: ProbeResults) -> str:
    """One-line, always-printed snapshot of every measured number.

    The CLI emits this BEFORE threshold validation, so it appears on
    both success and failure runs. Operator-facing: a single
    `PROBE SUMMARY:` line in verify_prod_probe_output.txt is what the
    .bat popup surfaces, so every number the gate checks against must
    be visible here. Shape:

        PROBE SUMMARY: ws_cap=<n>, rest_write_p50=<n>ms,
        rest_write_p95=<n>ms, rest_write_ok_rate=<0-1>,
        rate_ceiling=<n>rps, e2e_samples=<n>

    Missing / unmeasured fields render as 'n/a'. Intentionally
    compact so it fits one line in a MessageBox popup.
    """
    ws = results.ws_subscription or {}
    rw = results.rest_write_latency_ms or {}
    rl = results.rest_rate_limit or {}
    e2e = results.end_to_end_loop_ms or {}

    samples = int(rw.get("samples", 0) or 0)
    successful = int(rw.get("successful", 0) or 0)
    ok_rate_str = (
        f"{successful / samples:.2f}" if samples else "n/a"
    )

    return (
        SUMMARY_PREFIX
        + f"env={results.environment}, "
        + f"ws_cap={_fmt(ws.get('max_confirmed_tickers'))}, "
        + f"rest_write_p50={_fmt(rw.get('p50_ms'), unit='ms')}, "
        + f"rest_write_p95={_fmt(rw.get('p95_ms'), unit='ms')}, "
        + f"rest_write_ok_rate={ok_rate_str}, "
        + f"rate_ceiling={_fmt(rl.get('max_successful_rps'), unit='rps')}, "
        + f"e2e_samples={_fmt(e2e.get('samples'))}"
    )


def build_failed_detail_lines(reasons: list[str]) -> list[str]:
    """Each failure reason becomes its own `PROBE FAILED DETAIL: ...`
    line so the .bat can Select-String ALL of them into the popup
    (not just the first line of the ProbeFailure message)."""
    return [FAIL_DETAIL_PREFIX + r for r in reasons]


# ---------------------------------------------------------------------
# Error-response surface (operator-facing "why did Kalshi reject?")
# ---------------------------------------------------------------------

# Body length cap for the single-line popup render. Raw bodies from
# Kalshi include request-ID headers and other noise; truncate so one
# group still fits on one popup bullet.
_ERROR_BODY_MAX = 180


def parse_kalshi_error_body(body: str | None) -> str | None:
    """Pull 'code: message' from a Kalshi JSON error response.

    Kalshi returns JSON like:
        {"error": {"code": "insufficient_buying_power",
                   "message": "balance=$27, required=$0.50"}}
    The executor's LiveKalshiAPI surface wraps this; in probe we see the
    raw httpx.HTTPStatusError body. This parser is tolerant:

      - bytes / str input
      - nested under 'error' (Kalshi shape) or top-level
      - either field may be missing; we return whatever's available

    Returns None when the body is non-JSON or carries no usable fields,
    so the caller can fall back to the raw excerpt."""
    if body is None:
        return None
    text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else str(body)
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    err = data.get("error")
    if isinstance(err, dict):
        code = err.get("code") or err.get("kind") or ""
        msg = err.get("message") or err.get("detail") or ""
    else:
        code = data.get("code") or data.get("kind") or ""
        msg = data.get("message") or data.get("detail") or ""
    code = str(code).strip()
    msg = str(msg).strip()
    if code and msg:
        return f"{code}: {msg}"
    return code or msg or None


def _render_error_sample(sample: dict[str, Any]) -> str:
    """Format one ErrorCapture sample as
    `HTTP {status} "{code: message}"` or the best fallback if the
    status/body aren't present."""
    status = sample.get("http_status")
    body = sample.get("body_excerpt", "") or sample.get("message", "")
    parsed = parse_kalshi_error_body(body)
    detail = parsed if parsed else (body or "").strip()
    detail = detail.replace("\n", " ").replace("\r", " ")
    if len(detail) > _ERROR_BODY_MAX:
        detail = detail[:_ERROR_BODY_MAX - 3] + "..."
    if status is not None:
        prefix = f"HTTP {status}"
    else:
        prefix = sample.get("error_class", "error") or "error"
    return f'{prefix} "{detail}"' if detail else prefix


def summarize_error_groups(
    errors_summary: dict[str, Any] | None, *, label: str
) -> list[str]:
    """Turn an ErrorCapture.to_dict() blob into one popup-ready line per
    unique (status, body-signature) group, sorted by frequency.

    Shape of each returned string:
        "{label} {count}x HTTP {status} \"{code: message}\""
    e.g.
        "REST WRITE 100x HTTP 403 \"insufficient_buying_power: balance=$27, required=$0.50\""
        "E2E LOOP 30x HTTP 401 \"invalid_signature\""

    Empty list when there are no captured samples -- callers can
    safely iterate the return without guard checks. Groups are
    sorted by count descending so the most-common failure leads."""
    if not errors_summary:
        return []
    samples = errors_summary.get("samples") or []
    if not samples:
        return []
    ranked = sorted(
        samples,
        key=lambda s: int(s.get("count", 1) or 1),
        reverse=True,
    )
    out: list[str] = []
    for s in ranked:
        count = int(s.get("count", 1) or 1)
        body = _render_error_sample(s)
        out.append(f"{label} {count}x {body}")
    return out


def build_error_detail_lines(
    results: ProbeResults, *, block_labels: dict[str, str] | None = None
) -> list[str]:
    """Compose `PROBE FAILED DETAIL:` lines for every probe block that
    captured one or more errors. Called AFTER validate_prod_results
    (whose threshold-miss messages come first) so the operator sees:

        PROBE FAILED DETAIL: rest_write_latency_ms: success_rate=0.0% < required 80.0%
        PROBE FAILED DETAIL: REST WRITE 100x HTTP 403 "insufficient_buying_power: ..."
        PROBE FAILED DETAIL: E2E LOOP 30x HTTP 403 "insufficient_buying_power: ..."

    block_labels lets callers customise the display label per block;
    defaults to sensible prefixes."""
    labels = block_labels or {
        "rest_write_latency_ms": "REST WRITE",
        "end_to_end_loop_ms": "E2E LOOP",
        "rest_rate_limit": "RATE LIMIT",
    }
    lines: list[str] = []
    for block_name, label in labels.items():
        block = getattr(results, block_name, None)
        if not isinstance(block, dict):
            continue
        es = block.get("errors_summary")
        if not isinstance(es, dict):
            continue
        total_calls = int(es.get("total_calls", 0) or 0)
        total_errors = int(es.get("total_errors", 0) or 0)
        if total_errors <= 0:
            continue
        # Header line lets the operator see the failure ratio BEFORE
        # the per-group bullets. Matches the spec:
        #   "REST WRITE FAILURES: 100/100 failed"
        header = f"{label} FAILURES: {total_errors}/{total_calls} failed"
        lines.append(header)
        lines.extend(summarize_error_groups(es, label=label))
    return lines
