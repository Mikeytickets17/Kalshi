"""Per-tab integration tests for Step 4 content.

Every test:
  1. Boots a real uvicorn subprocess against a fresh SQLite DB
  2. Populates the DB with the deterministic fixture dataset
  3. Logs in over HTTP
  4. Hits the tab + the /{tab}/data endpoint + the detail endpoint
     (if applicable) + the CSV endpoint
  5. Asserts specific content renders / is present in the response

These tests extend the pattern in test_sse_e2e.py -- they use the same
cross-process SQLite visibility that bit Step 3 twice, so if this test
passes the operator's double-click verifier will too.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(host: str, port: int, timeout_s: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError(f"port {host}:{port} did not open in {timeout_s}s")


@pytest.fixture
def populated_dashboard(tmp_path):
    """Boot uvicorn against a fresh DB, populate it with fixture data,
    and yield (base_url, cookies, db_path)."""
    from tests.fixtures.dashboard_data import populate

    port = _free_port()
    db_path = tmp_path / "kalshi.db"
    password = "test-password-tabs"
    secret = "test-session-secret-tabs"

    # Populate BEFORE boot so lifespan opens a DB that already has
    # schema + fixture rows.
    populate(db_path)

    env = os.environ.copy()
    env["DASHBOARD_PASSWORD"] = password
    env["DASHBOARD_SESSION_SECRET"] = secret
    env["DASHBOARD_USERNAME"] = "admin"
    env["EVENT_STORE_PATH"] = str(db_path)
    env["PORT"] = str(port)
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")

    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn",
            "kalshi_arb.dashboard.main:app",
            "--host", "127.0.0.1", "--port", str(port),
            "--log-level", "warning", "--no-access-log",
        ],
        cwd=str(REPO_ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        _wait_for_port("127.0.0.1", port)
        base_url = f"http://127.0.0.1:{port}"
        login = httpx.post(
            f"{base_url}/login",
            data={"username": "admin", "password": password},
            follow_redirects=False, timeout=10.0,
        )
        assert login.status_code == 303, f"login failed: {login.status_code}"
        cookies = {"kalshi_dash_session": login.cookies["kalshi_dash_session"]}
        yield (base_url, cookies, db_path)
    finally:
        proc.terminate()
        try:
            out, _ = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate(timeout=5)
        if out:
            sys.stdout.write("\n--- uvicorn output ---\n")
            sys.stdout.write(out.decode(errors="replace"))


# ---------------- Overview ----------------


def test_overview_tab_renders_bot_status_and_tiles(populated_dashboard):
    base_url, cookies, _ = populated_dashboard
    r = httpx.get(f"{base_url}/overview", cookies=cookies, timeout=10.0)
    assert r.status_code == 200
    body = r.text
    # Bot status: fixture ends with kill_switch reset (tripped=False)
    # and has degraded events in last hour, so status is DEGRADED.
    assert "DEGRADED" in body, "bot status not rendered"
    # P&L tiles
    assert "Realized P&amp;L" in body
    assert "Estimated" in body
    assert "Opportunities today" in body
    # The fixture has net_cents summing to 265 -> $2.65 realized.
    assert "$2.65" in body
    # Recent lists have fixture tickers.
    assert "KXFED-DEC" in body or "KXBTC-NOV" in body
    # Decisions-per-minute chart canvas present.
    assert 'id="chart-decisions-per-minute"' in body


def test_overview_data_endpoint_matches_template(populated_dashboard):
    base_url, cookies, _ = populated_dashboard
    r = httpx.get(f"{base_url}/overview/data", cookies=cookies, timeout=10.0)
    assert r.status_code == 200
    j = r.json()
    assert j["bot_status"] in ("LIVE", "IDLE", "DEGRADED", "KILL-SWITCH")
    assert j["tiles"]["realized_cents"] == 265
    assert j["tiles"]["opportunities_today"] >= 5
    assert len(j["recent_opportunities"]) == 5
    assert len(j["recent_executions"]) == 5


# ---------------- Opportunities ----------------


def test_opportunities_tab_renders_all_rows(populated_dashboard):
    base_url, cookies, _ = populated_dashboard
    r = httpx.get(f"{base_url}/opportunities", cookies=cookies, timeout=10.0)
    assert r.status_code == 200
    body = r.text
    # Fixture has 12 total opportunities (5 emit + 7 skip).
    assert "Total matched: " in body
    assert "KXBTC-NOV" in body
    assert "KXETH-NOV" in body
    # Filter form present.
    assert 'id="opps-filter-form"' in body
    assert 'id="opps-csv-link"' in body
    # Rows are clickable.
    assert 'class="row-clickable"' in body
    # Decisions badged.
    assert ">emit<" in body
    assert ">skip_below_edge<" in body


def test_opportunities_filter_by_decision_emit(populated_dashboard):
    base_url, cookies, _ = populated_dashboard
    r = httpx.get(
        f"{base_url}/opportunities?decision=emit",
        cookies=cookies, timeout=10.0,
    )
    assert r.status_code == 200
    # Only 5 emit rows in the fixture.
    r2 = httpx.get(
        f"{base_url}/opportunities/data?decision=emit",
        cookies=cookies, timeout=10.0,
    )
    j = r2.json()
    assert j["total"] == 5
    assert all(row["decision"] == "emit" for row in j["rows"])


def test_opportunities_detail_drawer_payload(populated_dashboard):
    base_url, cookies, _ = populated_dashboard
    # Find an emitted opportunity so it has orders + fills + pnl.
    r = httpx.get(
        f"{base_url}/opportunities/data?decision=emit",
        cookies=cookies, timeout=10.0,
    )
    opp_id = r.json()["rows"][0]["id"]
    det = httpx.get(
        f"{base_url}/opportunities/{opp_id}/detail",
        cookies=cookies, timeout=10.0,
    )
    assert det.status_code == 200
    d = det.json()
    assert d["id"] == opp_id
    assert d["decision"] == "emit"
    assert d["book"]["sum_cents"] > 0
    assert d["sizer"]["final_size"] > 0
    assert len(d["orders"]) == 2
    assert any(o["side"] == "yes" for o in d["orders"])
    assert any(o["side"] == "no" for o in d["orders"])
    # Every order should have at least one fill in the fixture.
    for o in d["orders"]:
        assert len(o["fills"]) >= 1


def test_opportunities_csv_export_contains_all_rows(populated_dashboard):
    base_url, cookies, _ = populated_dashboard
    r = httpx.get(
        f"{base_url}/opportunities/export.csv",
        cookies=cookies, timeout=10.0,
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    lines = r.text.strip().split("\n")
    # 1 header + 12 rows.
    assert len(lines) == 13, f"expected 13 lines (1 header + 12 rows), got {len(lines)}: {lines[:3]}"
    assert lines[0].startswith("id,ticker,ts_iso")


# ---------------- Trades Taken ----------------


def test_trades_tab_renders_settled_and_open_rows(populated_dashboard):
    base_url, cookies, _ = populated_dashboard
    r = httpx.get(f"{base_url}/trades", cookies=cookies, timeout=10.0)
    assert r.status_code == 200
    body = r.text
    # 5 emitted opps = 5 trades in fixture.
    assert "Total: " in body
    assert "KXBTC-NOV" in body
    # Outcome badges rendered.
    assert ">win<" in body
    assert ">loss<" in body
    # CSV + filter present.
    assert 'id="trades-csv-link"' in body


def test_trades_data_aggregates_legs(populated_dashboard):
    base_url, cookies, _ = populated_dashboard
    r = httpx.get(f"{base_url}/trades/data", cookies=cookies, timeout=10.0)
    j = r.json()
    assert j["total"] == 5
    for row in j["rows"]:
        assert row["yes"]["count"] > 0
        assert row["no"]["count"] > 0
        assert row["outcome"] in ("win", "loss", "breakeven", "open")


def test_trade_detail_contains_both_legs_and_unwind(populated_dashboard):
    base_url, cookies, _ = populated_dashboard
    r = httpx.get(f"{base_url}/trades/data", cookies=cookies, timeout=10.0)
    opp_id = r.json()["rows"][0]["opportunity_id"]
    det = httpx.get(
        f"{base_url}/trades/{opp_id}/detail",
        cookies=cookies, timeout=10.0,
    )
    assert det.status_code == 200
    d = det.json()
    assert "legs" in d
    assert d["legs"]["yes"]["total_count"] > 0
    assert d["legs"]["no"]["total_count"] > 0
    assert "unwind" in d
    # Symmetric fixture -> no unwind needed.
    assert d["unwind"]["needed"] is False


def test_trades_csv_contains_both_fill_columns(populated_dashboard):
    base_url, cookies, _ = populated_dashboard
    r = httpx.get(
        f"{base_url}/trades/export.csv", cookies=cookies, timeout=10.0
    )
    assert r.status_code == 200
    header = r.text.strip().split("\n")[0]
    for col in ("yes_count", "yes_avg_price", "no_count", "no_avg_price",
                "outcome", "net_cents"):
        assert col in header, f"csv header missing {col}: {header}"


# ---------------- P&L ----------------


def test_pnl_tab_renders_stats_and_charts(populated_dashboard):
    base_url, cookies, _ = populated_dashboard
    r = httpx.get(f"{base_url}/pnl", cookies=cookies, timeout=10.0)
    assert r.status_code == 200
    body = r.text
    # Stats panel labels
    assert "Win rate" in body
    assert "Avg edge captured" in body
    assert "Max drawdown" in body
    # Chart canvases
    assert 'id="chart-equity-curve"' in body
    assert 'id="chart-daily-bars"' in body
    # Categories list populated from fixture markets table.
    assert "crypto" in body
    assert "econ" in body
    assert "weather" in body


def test_pnl_data_stats_match_fixture(populated_dashboard):
    base_url, cookies, _ = populated_dashboard
    r = httpx.get(f"{base_url}/pnl/data", cookies=cookies, timeout=10.0)
    j = r.json()
    assert j["stats"]["total_trades"] == 5
    assert j["stats"]["win_count"] == 3
    # 118 + 78 + -12 + -2 + 83 = 265 cents; curve last point == 265.
    assert j["equity_curve"][-1]["cum_cents"] == 265
    # 3 categories represented in the fixture.
    assert len(j["category_breakdown"]) == 3


# ---------------- System Health ----------------


def test_system_health_tab_renders_probe_and_degraded(populated_dashboard):
    base_url, cookies, _ = populated_dashboard
    r = httpx.get(f"{base_url}/system-health", cookies=cookies, timeout=10.0)
    assert r.status_code == 200
    body = r.text
    # Probe names from fixture
    assert "auth" in body
    assert "rest" in body
    assert "order_lifecycle" in body
    # Fail state surfaced
    assert "429 rate limited" in body
    # WS pool
    assert "KXBTC-NOV" in body
    # Degraded
    assert "ws_reconnect_storm" in body
    # Kill-switch history (2 rows)
    assert "fixture-trip" in body
    assert "fixture-reset" in body


def test_system_health_data_endpoint_populated(populated_dashboard):
    base_url, cookies, _ = populated_dashboard
    r = httpx.get(f"{base_url}/system-health/data", cookies=cookies, timeout=10.0)
    j = r.json()
    assert len(j["probes"]) == 4
    assert len(j["ws_pool"]) == 3
    assert len(j["degraded_events"]) == 2
    assert len(j["kill_switch_history"]) == 2
    assert j["kill_switch"] is not None


# ---------------- News (placeholder) ----------------


def test_news_tab_placeholder(populated_dashboard):
    base_url, cookies, _ = populated_dashboard
    r = httpx.get(f"{base_url}/news", cookies=cookies, timeout=10.0)
    assert r.status_code == 200
    assert "News module not yet deployed" in r.text


# ---------------- Drawer + empty state smoke tests ----------------


def test_opportunity_detail_404_for_unknown(populated_dashboard):
    base_url, cookies, _ = populated_dashboard
    r = httpx.get(
        f"{base_url}/opportunities/999999/detail",
        cookies=cookies, timeout=10.0,
    )
    assert r.status_code == 404


def test_empty_state_when_db_fresh(tmp_path):
    """A freshly-created DB (no fixture) should render empty-state
    copy ('no data yet') on every tab and NOT 500. Covers the
    regression where a blank table caused the page to crash."""
    from tests.fixtures.dashboard_data import populate as _unused  # noqa: F401
    port = _free_port()
    db_path = tmp_path / "empty.db"
    password = "empty-test"
    secret = "empty-secret"

    env = os.environ.copy()
    env["DASHBOARD_PASSWORD"] = password
    env["DASHBOARD_SESSION_SECRET"] = secret
    env["DASHBOARD_USERNAME"] = "admin"
    env["EVENT_STORE_PATH"] = str(db_path)
    env["PORT"] = str(port)
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")

    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn",
            "kalshi_arb.dashboard.main:app",
            "--host", "127.0.0.1", "--port", str(port),
            "--log-level", "warning", "--no-access-log",
        ],
        cwd=str(REPO_ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        _wait_for_port("127.0.0.1", port)
        base_url = f"http://127.0.0.1:{port}"
        login = httpx.post(
            f"{base_url}/login",
            data={"username": "admin", "password": password},
            follow_redirects=False, timeout=10.0,
        )
        cookies = {"kalshi_dash_session": login.cookies["kalshi_dash_session"]}
        for slug, expected in [
            ("overview", "No opportunities yet"),
            ("opportunities", "No opportunities match the current filters"),
            ("trades", "No trades match the current filters"),
            ("pnl", "No realized P"),
            ("system-health", "No probe runs recorded"),
        ]:
            r = httpx.get(f"{base_url}/{slug}", cookies=cookies, timeout=10.0)
            assert r.status_code == 200, f"{slug} returned {r.status_code}"
            assert expected in r.text, (
                f"{slug} missing empty-state copy; got: {r.text[:500]}"
            )
    finally:
        proc.terminate()
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate(timeout=5)
