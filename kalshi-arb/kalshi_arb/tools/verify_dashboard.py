"""One-shot dashboard verifier.

Run this AFTER the dashboard is up (start_dashboard.bat is running and
printed a tunnel URL). It:

  1. Reads the tunnel URL from dashboard_url.txt.
  2. Reads the dashboard password from .dashboard_creds.
  3. Logs in over HTTPS through the tunnel.
  4. Hits /healthz and verifies fields are present.
  5. Inserts 5 synthetic events into the local event store.
  6. Polls /events/poll until all 5 events come back (proves the
     bot-write -> dashboard-read pipeline works through the tunnel).
  7. Prints a PASS/FAIL summary.

You don't need to open a browser, click anything, or interpret what
the screen shows. This script does all four gate checks and tells you
if they pass.

Run:
    python -m kalshi_arb.tools.verify_dashboard

Exit code 0 = all checks passed; non-zero = something failed (the
specific failure is printed).
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import httpx

from .. import log
from ..store import EventStore, SqliteBackend
from .simulate_events import _insert_one
import random

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
        # ---- 1) /healthz publicly reachable ----
        dashboard_health: dict = {}
        try:
            r = await client.get("/healthz")
            dashboard_health = r.json() if r.status_code == 200 else {}
            ok = r.status_code == 200 and dashboard_health.get("status") == "ok"
            _check("Tunnel reachable + /healthz returns 200",
                   ok, f"status={r.status_code}")
            if not ok:
                failures.append("/healthz")
        except Exception as exc:  # noqa: BLE001
            _check("Tunnel reachable + /healthz returns 200",
                   False, f"exception: {exc}")
            failures.append("/healthz")
            return 1  # nothing else can work

        # ---- 1b) Dashboard's event-store path matches the verifier's ----
        from .._paths import default_event_store_path
        verifier_path = default_event_store_path()
        dashboard_path_str = dashboard_health.get("event_store_path") or ""
        if dashboard_path_str:
            try:
                dashboard_path = Path(dashboard_path_str).resolve()
            except Exception:
                dashboard_path = Path(dashboard_path_str)
            paths_match = dashboard_path == verifier_path.resolve()
            _check(
                "Dashboard and verifier point at the same SQLite file",
                paths_match,
                f"dashboard={dashboard_path} verifier={verifier_path}",
            )
            if not paths_match:
                failures.append("path-mismatch")
                print()
                print("  --> The dashboard process is reading a DIFFERENT")
                print("      file from the verifier.  Most likely cause: the")
                print("      dashboard was started in a different working")
                print("      directory before the path-fix shipped.  Stop")
                print("      the launcher window (close it / Ctrl+C) and")
                print("      double-click start_dashboard.bat again so the")
                print("      new code picks up the shared absolute path.")
                print()
        else:
            _check(
                "Dashboard and verifier point at the same SQLite file",
                False,
                "dashboard /healthz did not include event_store_path -- "
                "old dashboard build still running; restart the launcher.",
            )
            failures.append("path-mismatch")

        # ---- 2) Login over HTTPS ----
        try:
            r = await client.post(
                "/login",
                data={"username": "admin", "password": password},
            )
            ok = r.status_code == 303 and r.headers.get("location") == "/overview"
            _check("Login with admin / .dashboard_creds password",
                   ok, f"status={r.status_code} location={r.headers.get('location')}")
            if not ok:
                failures.append("login")
                return 2
            cookie = r.cookies.get("kalshi_dash_session")
            assert cookie, "no session cookie returned on successful login"
        except Exception as exc:  # noqa: BLE001
            _check("Login with admin / .dashboard_creds password",
                   False, f"exception: {exc}")
            failures.append("login")
            return 2

        # ---- 3) All six tabs reachable when authenticated ----
        tab_results = []
        for slug in ("overview", "opportunities", "trades", "pnl",
                     "system-health", "news"):
            try:
                r = await client.get(f"/{slug}")
                tab_results.append((slug, r.status_code))
            except Exception as exc:  # noqa: BLE001
                tab_results.append((slug, f"exception: {exc}"))
        all_ok = all(rs == 200 for _, rs in tab_results)
        detail = ", ".join(f"{s}={c}" for s, c in tab_results)
        _check("All six tabs return 200 when authed", all_ok, detail)
        if not all_ok:
            failures.append("tabs")

        # ---- 4) End-to-end SSE pipeline: insert events, watch them appear ----
        # Pre-read the current high-water mark so we only count NEW events.
        try:
            r = await client.get("/events/poll?since_id=0&limit=1")
            data = r.json()
            current_max_id = max(
                (ch["id"] for ch in data.get("changes", [])), default=0
            )
        except Exception as exc:  # noqa: BLE001
            _check("Pre-read change_log high-water mark",
                   False, f"exception: {exc}")
            failures.append("pre-read")
            return 4
        _check("Pre-read change_log high-water mark",
               True, f"current max id = {current_max_id}")

        # Insert 5 events directly via the local EventStore. This is the
        # exact path the bot uses; we're impersonating it.
        # MUST match the path the dashboard opened. Both go through the
        # shared resolver so a CWD difference between processes can't
        # silently point them at different files (which is exactly the
        # bug that bit step 3 the first time).
        from .._paths import default_event_store_path
        store_path = default_event_store_path()
        print(f"  using event store: {store_path}")
        try:
            store = EventStore(SqliteBackend(store_path))
            await store.start()
            rng = random.Random()
            for i in range(5):
                await _insert_one(store, rng, i + 1000)
            # let the writer drain
            await asyncio.sleep(0.5)
            stats = store.stats()
            await store.stop()
            _check("Inserted 5 synthetic events into local event store",
                   stats["written_total"] >= 5,
                   f"written={stats['written_total']} dropped={stats['dropped_total']}")
        except Exception as exc:  # noqa: BLE001
            _check("Inserted 5 synthetic events into local event store",
                   False, f"exception: {exc}")
            failures.append("insert")
            return 5

        # Wait up to 10s for those 5 events to be visible to the
        # dashboard via /events/poll. The change-capture task runs at
        # 1s cadence, so 10s is generous.
        deadline = time.monotonic() + 10.0
        new_count = 0
        new_ids: list[int] = []
        while time.monotonic() < deadline:
            try:
                r = await client.get(
                    f"/events/poll?since_id={current_max_id}&limit=200"
                )
                changes = r.json().get("changes", [])
                new_ids = [ch["id"] for ch in changes]
                new_count = len(new_ids)
                if new_count >= 5:
                    break
            except Exception as exc:  # noqa: BLE001
                _log.warning("verify.poll_failed", error=str(exc))
            await asyncio.sleep(0.5)

        ok = new_count >= 5
        _check("End-to-end pipeline: events visible via tunnel",
               ok,
               f"saw {new_count} new events with ids {new_ids[:5]}{'...' if new_count > 5 else ''}")
        if not ok:
            failures.append("e2e")
            # Diagnostic: ask the dashboard how many change_log rows IT
            # currently sees on its connection.  If the count is 0 (or
            # equals the pre-insert count), the dashboard's connection
            # isn't seeing the verifier's writes -- typically a Windows
            # WAL visibility issue or a stale dashboard process.
            try:
                r2 = await client.get("/healthz")
                hb = r2.json() if r2.status_code == 200 else {}
                d_count = hb.get("change_log_count")
                v_count: int | None
                try:
                    v_store = EventStore(SqliteBackend(verifier_path))
                    v_store.connect()
                    v_count = v_store.change_log_count()
                    v_store.backend.close()
                except Exception as exc:  # noqa: BLE001
                    v_count = None
                    print(f"  diag: verifier could not read its own count: {exc}")
                print()
                print(f"  diag: dashboard sees {d_count} rows in change_log")
                print(f"  diag: verifier  sees {v_count} rows in change_log")
                if d_count == 0 and (v_count or 0) > 0:
                    print("  --> Path was OK but dashboard's connection")
                    print("      sees none of the writes.  Restart the")
                    print("      launcher window so its DB connection")
                    print("      reopens against the WAL-mode file.")
                elif d_count == v_count and (d_count or 0) > 0:
                    print("  --> Both processes see the rows but the SSE")
                    print("      pipeline isn't surfacing them.  Likely")
                    print("      the change-capture task is stuck or the")
                    print("      /events/poll auth/path is broken.")
            except Exception as exc:  # noqa: BLE001
                print(f"  diag: failed to fetch /healthz for diagnostics: {exc}")

    print()
    print("=" * 70)
    if failures:
        print(f"  RESULT: FAIL ({len(failures)} of 4+ checks failed: "
              f"{', '.join(failures)})")
        print("  Re-run after fixing or paste this output to Claude.")
        print("=" * 70)
        return 10
    print("  RESULT: PASS  -- all four gate checks succeeded.")
    print("  Step 3 verified end-to-end: tunnel + auth + tabs + live SSE.")
    print("=" * 70)
    return 0


def main() -> int:
    repo = _repo_root()
    url_file = repo / "dashboard_url.txt"
    creds_file = repo / ".dashboard_creds"
    url = _read_text(url_file)
    password = _read_text(creds_file)
    return asyncio.run(_verify(url, password, repo))


if __name__ == "__main__":
    raise SystemExit(main())
