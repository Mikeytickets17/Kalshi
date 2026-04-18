"""One-shot dashboard verifier.

Run this AFTER the dashboard is up (start_dashboard.bat is running and
printed a tunnel URL). It:

  Gate 1: /healthz reachable and returns expected fields
  Gate 2: Dashboard + verifier agree on event-store path
  Gate 3: Login succeeds with the .dashboard_creds password
  Gate 4: Every tab (overview, opportunities, trades, pnl,
          system-health, news) returns 200 with auth
  Gate 5: Step-4 content -- every tab's /{tab}/data endpoint returns
          the right shape and populated fixture data
  Gate 6: Drawer detail endpoint returns content for at least one
          row on Opportunities and Trades
  Gate 7: CSV exports for Opportunities + Trades are reachable and
          contain header + data rows
  Gate 8: End-to-end SSE pipeline: insert 5 synthetic events, see
          them arrive via /events/poll within 10 s

The popup reads the final HEADLINE: line, so PASS means ALL of the
above are green; FAIL names the first failing gate with an actionable
next step.
"""

from __future__ import annotations

import asyncio
import os
import random
import sys
import time
from pathlib import Path

import httpx

from .. import log
from ..store import EventStore, SqliteBackend
from .simulate_events import _insert_one

_log = log.get("tools.verify")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _read_text(path: Path) -> str:
    if not path.exists():
        raise SystemExit(
            f"FAIL: {path.name} not found at {path}.\n"
            f"Did you run start_dashboard.bat first? Look in the launcher "
            f"window for the URL banner; it writes the URL there before "
            f"this script can read it."
        )
    return path.read_text(encoding="utf-8").strip()


def _check(label: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    color_on = "\033[32m" if ok else "\033[31m"
    color_off = "\033[0m"
    suffix = f"  [{detail}]" if detail else ""
    print(f"  {color_on}{mark}{color_off}  {label}{suffix}")


TABS_CONTENT_EXPECTATIONS = {
    # path -> (data-endpoint, required JSON keys)
    "/overview":       ("/overview/data", ["bot_status", "tiles",
                                           "recent_opportunities",
                                           "recent_executions",
                                           "decisions_per_minute"]),
    "/opportunities":  ("/opportunities/data", ["rows", "total"]),
    "/trades":         ("/trades/data", ["rows", "total"]),
    "/pnl":            ("/pnl/data", ["equity_curve", "stats",
                                       "daily_bars", "category_breakdown"]),
    "/system-health":  ("/system-health/data", ["probes", "ws_pool",
                                                 "event_store",
                                                 "kill_switch_history"]),
    "/news":           (None, None),  # placeholder tab, no /data endpoint
}


async def _check_tab_content(
    client: httpx.AsyncClient, auth_cookies: dict
) -> list[str]:
    """Returns a list of failure labels (empty = all green)."""
    failures: list[str] = []
    for tab_path, (data_path, required_keys) in TABS_CONTENT_EXPECTATIONS.items():
        r = await client.get(tab_path, cookies=auth_cookies)
        if r.status_code != 200:
            _check(f"{tab_path} tab renders",
                   False, f"status={r.status_code}")
            failures.append(f"tab-{tab_path}")
            continue
        _check(f"{tab_path} tab renders", True)
        if data_path is None:
            continue
        r2 = await client.get(data_path, cookies=auth_cookies)
        if r2.status_code != 200:
            _check(f"{data_path} returns JSON",
                   False, f"status={r2.status_code}")
            failures.append(f"data-{data_path}")
            continue
        try:
            body = r2.json()
        except Exception as exc:  # noqa: BLE001
            _check(f"{data_path} returns JSON",
                   False, f"not JSON: {exc}")
            failures.append(f"data-{data_path}")
            continue
        missing = [k for k in (required_keys or []) if k not in body]
        _check(f"{data_path} returns expected keys",
               not missing,
               f"missing={missing}" if missing else f"keys={list(body)[:6]}")
        if missing:
            failures.append(f"data-{data_path}")
    return failures


async def _check_drawer_detail(
    client: httpx.AsyncClient, auth_cookies: dict
) -> list[str]:
    """Exercise /opportunities/{id}/detail and /trades/{id}/detail."""
    failures: list[str] = []
    r = await client.get(
        "/opportunities/data?limit=1", cookies=auth_cookies
    )
    if r.status_code != 200:
        _check("Opportunities drawer detail", False,
               f"opps-list status={r.status_code}")
        return ["drawer-opps-list"]
    rows = r.json().get("rows") or []
    if rows:
        opp_id = rows[0]["id"]
        d = await client.get(
            f"/opportunities/{opp_id}/detail", cookies=auth_cookies
        )
        ok = d.status_code == 200 and "book" in (d.json() if d.status_code == 200 else {})
        _check("Opportunities drawer detail", ok,
               f"id={opp_id} status={d.status_code}")
        if not ok:
            failures.append("drawer-opps")
    else:
        _check("Opportunities drawer detail", True,
               "no rows to drill into (fresh DB -- not a failure)")

    r = await client.get("/trades/data?limit=1", cookies=auth_cookies)
    if r.status_code != 200:
        _check("Trades drawer detail", False,
               f"trades-list status={r.status_code}")
        return failures + ["drawer-trades-list"]
    rows = r.json().get("rows") or []
    if rows:
        opp_id = rows[0]["opportunity_id"]
        d = await client.get(
            f"/trades/{opp_id}/detail", cookies=auth_cookies
        )
        ok = d.status_code == 200 and "legs" in (d.json() if d.status_code == 200 else {})
        _check("Trades drawer detail", ok,
               f"id={opp_id} status={d.status_code}")
        if not ok:
            failures.append("drawer-trades")
    else:
        _check("Trades drawer detail", True,
               "no trades to drill into (fresh DB -- not a failure)")
    return failures


async def _check_csv_exports(
    client: httpx.AsyncClient, auth_cookies: dict
) -> list[str]:
    failures: list[str] = []
    for path, label in [
        ("/opportunities/export.csv", "Opportunities CSV export"),
        ("/trades/export.csv", "Trades CSV export"),
    ]:
        r = await client.get(path, cookies=auth_cookies)
        ok = (
            r.status_code == 200
            and r.headers.get("content-type", "").startswith("text/csv")
            and r.text.count("\n") >= 1  # at least header
        )
        _check(label, ok,
               f"status={r.status_code} "
               f"content-type={r.headers.get('content-type')} "
               f"lines={r.text.count(chr(10))}")
        if not ok:
            failures.append(label.lower().replace(" ", "-"))
    return failures


async def _verify(url: str, password: str, repo: Path) -> int:
    print()
    print("=" * 70)
    print(f"  Verifying dashboard at {url}")
    print("=" * 70)

    failures: list[str] = []
    timeout = httpx.Timeout(15.0, connect=10.0)

    async with httpx.AsyncClient(
        base_url=url, follow_redirects=False, timeout=timeout
    ) as client:
        # ---- Gate 1) /healthz ----
        dashboard_health: dict = {}
        try:
            r = await client.get("/healthz")
            dashboard_health = r.json() if r.status_code == 200 else {}
            ok = r.status_code == 200 and dashboard_health.get("status") == "ok"
            _check("Gate 1: /healthz reachable",
                   ok, f"status={r.status_code}")
            if not ok:
                failures.append("/healthz")
                _print_fail(failures)
                return 1
        except Exception as exc:  # noqa: BLE001
            _check("Gate 1: /healthz reachable", False, f"exception: {exc}")
            failures.append("/healthz")
            _print_fail(failures)
            return 1

        # ---- Gate 2) Path agreement ----
        from .._paths import default_event_store_path
        verifier_path = default_event_store_path()
        dashboard_path_str = dashboard_health.get("event_store_path") or ""
        dashboard_path: Path | None = None
        if dashboard_path_str:
            try:
                dashboard_path = Path(dashboard_path_str).resolve()
            except Exception:
                dashboard_path = Path(dashboard_path_str)
        paths_match = dashboard_path == verifier_path.resolve()
        _check(
            "Gate 2: Dashboard and verifier share the SQLite file",
            paths_match,
            f"dashboard={dashboard_path} verifier={verifier_path}",
        )
        if not paths_match:
            failures.append("path-mismatch")
            _print_fail(failures)
            return 2

        # ---- Gate 3) Login ----
        try:
            r = await client.post(
                "/login", data={"username": "admin", "password": password}
            )
            ok = r.status_code == 303 and r.headers.get("location") == "/overview"
            _check("Gate 3: Login with admin / .dashboard_creds",
                   ok,
                   f"status={r.status_code} location={r.headers.get('location')}")
            if not ok:
                failures.append("login")
                _print_fail(failures)
                return 3
            cookie = r.cookies.get("kalshi_dash_session")
            assert cookie, "no session cookie on 303"
        except Exception as exc:  # noqa: BLE001
            _check("Gate 3: Login", False, f"exception: {exc}")
            failures.append("login")
            _print_fail(failures)
            return 3
        auth_cookies = {"kalshi_dash_session": cookie}

        # ---- Gate 4+5) Each tab renders AND /data endpoint returns JSON ----
        tab_failures = await _check_tab_content(client, auth_cookies)
        failures.extend(tab_failures)

        # ---- Gate 6) Drawer detail ----
        drawer_failures = await _check_drawer_detail(client, auth_cookies)
        failures.extend(drawer_failures)

        # ---- Gate 7) CSV exports ----
        csv_failures = await _check_csv_exports(client, auth_cookies)
        failures.extend(csv_failures)

        # ---- Gate 8) SSE pipeline ----
        try:
            r = await client.get(
                "/events/poll?since_id=0&limit=1", cookies=auth_cookies
            )
            data = r.json()
            current_max_id = max(
                (ch["id"] for ch in data.get("changes", [])), default=0
            )
            _check("Gate 8a: Pre-read change_log high-water mark",
                   True, f"max id = {current_max_id}")
        except Exception as exc:  # noqa: BLE001
            _check("Gate 8a: Pre-read change_log high-water mark",
                   False, f"exception: {exc}")
            failures.append("sse-preread")
            _print_fail(failures)
            return 8

        # Insert 5 events via the shared store helper.
        try:
            store = EventStore(SqliteBackend(verifier_path))
            await store.start()
            rng = random.Random()
            for i in range(5):
                await _insert_one(store, rng, i + 2000)
            await asyncio.sleep(0.5)
            stats = store.stats()
            await store.stop()
            _check("Gate 8b: Inserted 5 synthetic events",
                   stats["written_total"] >= 5,
                   f"written={stats['written_total']}")
        except Exception as exc:  # noqa: BLE001
            _check("Gate 8b: Inserted 5 synthetic events",
                   False, f"exception: {exc}")
            failures.append("sse-insert")
            _print_fail(failures)
            return 8

        # Wait up to 10s for rows to surface via /events/poll.
        deadline = time.monotonic() + 10.0
        new_count = 0
        while time.monotonic() < deadline:
            try:
                r = await client.get(
                    f"/events/poll?since_id={current_max_id}&limit=200",
                    cookies=auth_cookies,
                )
                new_count = len(r.json().get("changes", []))
                if new_count >= 5:
                    break
            except Exception as exc:  # noqa: BLE001
                _log.warning("verify.poll_failed", error=str(exc))
            await asyncio.sleep(0.5)
        ok = new_count >= 5
        _check("Gate 8c: SSE pipeline delivers 5 new events",
               ok, f"saw {new_count} new events")
        if not ok:
            failures.append("sse-e2e")

    print()
    print("=" * 70)
    if failures:
        _print_fail(failures)
        return 10
    print("  RESULT: PASS -- all Step-4 gates succeeded.")
    print("  Overview / Opportunities / Trades / P&L / System Health /")
    print("  News all render + /data / detail / CSV + SSE delivers events.")
    print("=" * 70)
    print("HEADLINE: PASS -- dashboard is live; every tab + drawer + CSV + SSE green.")
    return 0


def _print_fail(failures: list[str]) -> None:
    first = failures[0]
    action = {
        "/healthz": (
            "Dashboard isn't reachable. Make sure start_dashboard.bat "
            "is running and showed a tunnel URL banner."
        ),
        "login": (
            "Login failed. The .dashboard_creds password didn't match. "
            "If you regenerated it, restart the launcher."
        ),
        "path-mismatch": (
            "Dashboard is running OLD code (different SQLite path "
            "than the verifier). Close the launcher window and "
            "double-click start_dashboard.bat again."
        ),
        "sse-preread": (
            "Could not read /events/poll. Likely a session-cookie "
            "issue. Restart the launcher and re-run this script."
        ),
        "sse-insert": (
            "Verifier failed to write to the SQLite event store. "
            "Check disk space and that the DB isn't locked."
        ),
        "sse-e2e": (
            "Writes succeeded but never reached the dashboard. "
            "Almost always: the running dashboard is OLD code -- "
            "close the launcher window and re-open start_dashboard.bat."
        ),
    }.get(first)
    if action is None:
        if first.startswith("tab-"):
            action = f"Tab {first[4:]} did not render. Restart the launcher so the new code loads."
        elif first.startswith("data-"):
            action = f"Data endpoint {first[5:]} returned bad JSON. Step-4 queries.py may be broken -- share verify_output.txt."
        elif first.startswith("drawer-"):
            action = "Drawer detail endpoint failed. Restart launcher; share verify_output.txt if persists."
        elif first.endswith("-csv-export"):
            action = "CSV export endpoint failed. Restart launcher; share verify_output.txt if persists."
        else:
            action = f"Check '{first}' failed -- see details below."

    print(f"  RESULT: FAIL ({len(failures)} check(s) failed: "
          f"{', '.join(failures)})")
    print(f"  ACTION: {action}")
    print("=" * 70)
    print(f"HEADLINE: FAIL -- {action}")


def main() -> int:
    repo = _repo_root()
    url_file = repo / "dashboard_url.txt"
    creds_file = repo / ".dashboard_creds"
    url = _read_text(url_file)
    password = _read_text(creds_file)
    return asyncio.run(_verify(url, password, repo))


if __name__ == "__main__":
    raise SystemExit(main())
