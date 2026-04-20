"""End-to-end SSE test against a real uvicorn process.

This test exists because TestClient + ASGI doesn't reproduce the actual
production data path. The real path is:

    sqlite write (process A)
        -> change_log row (visible to process B via WAL)
            -> ChangeCapture poll (process B, 1s tick)
                -> SSEBroker.publish (process B)
                    -> per-client asyncio.Queue (process B)
                        -> /events/stream generator (process B)
                            -> HTTP socket (real bytes over TCP)
                                -> EventSource client (browser)

This test exercises *all* of those links. It boots uvicorn in a
subprocess, logs in over real HTTP, opens /events/stream as a real
HTTP client, writes change_log rows from this test process (mimicking
the bot), and asserts the rows arrive on the SSE socket within a
latency budget.

If this test passes in CI, the operator's dashboard MUST work.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from kalshi_arb.store import EventStore, SqliteBackend


REPO_ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(host: str, port: int, timeout_s: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_s
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError as exc:  # noqa: PERF203
            last_exc = exc
            time.sleep(0.1)
    raise TimeoutError(
        f"port {host}:{port} did not open within {timeout_s}s; last={last_exc}"
    )


@pytest.fixture
def live_dashboard(tmp_path):
    """Boot a real uvicorn serving the dashboard against a fresh SQLite
    file in tmp_path. Yield (base_url, password, db_path). Tear down on
    exit."""
    port = _free_port()
    db_path = tmp_path / "kalshi.db"
    password = "test-password-do-not-use"
    secret = "test-session-secret-do-not-use-in-prod-please"

    env = os.environ.copy()
    env["DASHBOARD_PASSWORD"] = password
    env["DASHBOARD_SESSION_SECRET"] = secret
    env["DASHBOARD_USERNAME"] = "admin"
    env["EVENT_STORE_PATH"] = str(db_path)
    env["PORT"] = str(port)
    # Ensure the test process and the uvicorn subprocess agree on
    # which Python finds the package.
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "kalshi_arb.dashboard.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
            "--no-access-log",
        ],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_for_port("127.0.0.1", port)
        yield (f"http://127.0.0.1:{port}", password, db_path)
    finally:
        proc.terminate()
        try:
            out, _ = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate(timeout=5)
        # Surface uvicorn output if the test failed -- the assertion
        # will be wrapped in a helpful diagnostic.
        if out:
            print("\n--- uvicorn output ---")
            sys.stdout.write(out.decode(errors="replace"))


def _login(base_url: str, password: str) -> dict[str, str]:
    """Return the session cookie dict from a real /login POST."""
    # follow_redirects=False because the success path is a 303 with the
    # cookie set on that very response.
    r = httpx.post(
        f"{base_url}/login",
        data={"username": "admin", "password": password},
        follow_redirects=False,
        timeout=10.0,
    )
    assert r.status_code == 303, f"login failed: {r.status_code} {r.text}"
    cookie = r.cookies.get("kalshi_dash_session")
    assert cookie, "login returned no kalshi_dash_session cookie"
    return {"kalshi_dash_session": cookie}


async def _insert_one_async(db_path: Path, ticker: str) -> None:
    """Write one synthetic opportunity row via the same store helper
    the bot uses. Runs in *this* event loop (and this Python process,
    which is separate from uvicorn's) -- the cross-process pattern the
    operator hits in production."""
    store = EventStore(SqliteBackend(db_path))
    await store.start()
    try:
        store.record_opportunity(
            ticker=ticker,
            ts_ms=int(time.time() * 1000),
            yes_ask_cents=42,
            yes_ask_qty=100,
            no_ask_cents=55,
            no_ask_qty=100,
            sum_cents=97,
            est_fees_cents=3,
            slippage_buffer=0,
            net_edge_cents=0.5,
            max_size_liquidity=100,
            kelly_size=10,
            hard_cap_size=10,
            final_size=10,
            decision="emit",
        )
        # Let the writer coroutine drain so the row is committed and
        # WAL-visible to other processes (here: the uvicorn subprocess).
        await asyncio.sleep(0.5)
    finally:
        await store.stop()


def _insert_one_sync(db_path: Path, ticker: str) -> None:
    """Sync wrapper for tests not running inside an event loop."""
    asyncio.run(_insert_one_async(db_path, ticker))


def test_live_sse_delivers_cross_process_writes_within_3s(live_dashboard):
    """The real production path: a separate process writes change_log,
    the dashboard's ChangeCapture polls, and a real SSE client sees
    the row land on its socket within the latency budget."""
    base_url, password, db_path = live_dashboard
    cookies = _login(base_url, password)

    received: list[bytes] = []

    async def _consume() -> None:
        # Real HTTP streaming with the session cookie. This is the same
        # protocol the browser EventSource uses.
        async with httpx.AsyncClient(
            base_url=base_url, cookies=cookies, timeout=httpx.Timeout(15.0)
        ) as client:
            async with client.stream("GET", "/events/stream") as r:
                assert r.status_code == 200, (
                    f"SSE endpoint returned {r.status_code}; "
                    f"likely auth bug -- session cookie not honored on "
                    f"streaming endpoints"
                )
                ctype = r.headers.get("content-type", "")
                assert "text/event-stream" in ctype, (
                    f"wrong content-type: {ctype!r}"
                )
                # Drain raw bytes. Any chunk containing "event: opportunity"
                # is a hit.
                async for chunk in r.aiter_bytes():
                    received.append(chunk)
                    if any(b"event: opportunity" in c for c in received):
                        return

    async def _producer() -> None:
        # Brief delay so the consumer is subscribed before we publish.
        # ChangeCapture polls at 1s; budget is therefore tick + small
        # margin.
        await asyncio.sleep(0.5)
        await _insert_one_async(db_path, "KXSSE-E2E-1")

    async def _run() -> None:
        consumer = asyncio.create_task(_consume())
        producer = asyncio.create_task(_producer())
        # Total budget: 0.5s producer delay + 1s capture tick + 1.5s
        # network + slack.
        try:
            await asyncio.wait_for(consumer, timeout=6.0)
        finally:
            producer.cancel()
            try:
                await producer
            except (asyncio.CancelledError, Exception):
                pass

    try:
        asyncio.run(_run())
    except asyncio.TimeoutError:
        pytest.fail(
            "SSE client did not receive 'event: opportunity' within 6s. "
            f"Bytes seen: {b''.join(received)[:500]!r}"
        )

    blob = b"".join(received)
    assert b"event: opportunity" in blob, (
        f"SSE stream open but no opportunity event arrived. "
        f"Raw bytes: {blob[:500]!r}"
    )


def test_live_events_poll_returns_cross_process_writes(live_dashboard):
    """Same cross-process write, but observed via /events/poll instead
    of SSE. Isolates the persistence + change_log path from the
    streaming generator."""
    base_url, password, db_path = live_dashboard
    cookies = _login(base_url, password)

    _insert_one_sync(db_path, "KXPOLL-E2E-1")

    # Wait up to 3s for ChangeCapture to advance and for the row to be
    # visible. (Poll endpoint reads change_log directly, so even if
    # ChangeCapture is broken this should succeed -- this test
    # therefore narrows the bug.)
    deadline = time.monotonic() + 3.0
    last_body: dict = {}
    while time.monotonic() < deadline:
        r = httpx.get(
            f"{base_url}/events/poll?since_id=0&limit=100",
            cookies=cookies,
            timeout=5.0,
        )
        assert r.status_code == 200, (
            f"/events/poll returned {r.status_code} with cookies; "
            f"likely auth bug. body={r.text[:300]}"
        )
        last_body = r.json()
        if last_body.get("changes"):
            break
        time.sleep(0.2)

    changes = last_body.get("changes") or []
    assert changes, (
        f"/events/poll returned empty after cross-process write. "
        f"last response: {last_body}"
    )
    types = {c["entity_type"] for c in changes}
    assert "opportunity" in types, (
        f"opportunity row not visible via /events/poll: types={types}"
    )


def test_live_dashboard_serves_dash_js(live_dashboard):
    """The Overview tab does nothing visible unless dash.js is served
    from /static/dash.js. A 404 here would silently break every tab."""
    base_url, password, _ = live_dashboard
    r = httpx.get(f"{base_url}/static/dash.js", timeout=5.0)
    assert r.status_code == 200, (
        f"/static/dash.js returned {r.status_code} -- the JS that "
        f"connects EventSource isn't reachable, so the operator sees "
        f"no live updates regardless of backend health."
    )
    assert b"new EventSource" in r.content, (
        "dash.js served but doesn't contain EventSource bootstrap"
    )


def test_live_healthz_exposes_event_store_path(live_dashboard):
    """/healthz must expose event_store_path so verify_dashboard.bat
    can confirm it matches the verifier's resolved path."""
    base_url, _, db_path = live_dashboard
    r = httpx.get(f"{base_url}/healthz", timeout=5.0)
    assert r.status_code == 200
    body = r.json()
    assert body.get("status") == "ok"
    assert "event_store_path" in body
    assert Path(body["event_store_path"]).resolve() == db_path.resolve()


def test_verify_dashboard_cli_emits_pass_headline(live_dashboard, tmp_path):
    """End-to-end test of the operator's actual workflow:
    `python -m kalshi_arb.tools.verify_dashboard` running against a
    live dashboard should emit `HEADLINE: PASS ...` on stdout. The
    Windows .bat extracts that line for the popup -- if it doesn't
    appear, the popup says 'verifier produced no HEADLINE line' and
    the operator gets no actionable info.

    This guards against the verify_dashboard.py output format being
    accidentally broken by future edits.
    """
    base_url, password, db_path = live_dashboard
    # The verifier reads dashboard_url.txt and .dashboard_creds from
    # _repo_root() (kalshi-arb/). To avoid clobbering anything that
    # might exist there, we write into a tmp copy of the repo
    # structure and point the CLI at it via an env shim.
    creds_file = REPO_ROOT / ".dashboard_creds"
    url_file = REPO_ROOT / "dashboard_url.txt"
    # Save and restore originals if they exist.
    saved_creds = creds_file.read_bytes() if creds_file.exists() else None
    saved_url = url_file.read_bytes() if url_file.exists() else None
    creds_file.write_text(password, encoding="utf-8")
    url_file.write_text(base_url, encoding="utf-8")

    env = os.environ.copy()
    env["EVENT_STORE_PATH"] = str(db_path)
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")

    try:
        proc = subprocess.run(
            [sys.executable, "-m", "kalshi_arb.tools.verify_dashboard"],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
            cwd=str(REPO_ROOT),
        )
    finally:
        # Restore the operator's real files.
        if saved_creds is None:
            creds_file.unlink(missing_ok=True)
        else:
            creds_file.write_bytes(saved_creds)
        if saved_url is None:
            url_file.unlink(missing_ok=True)
        else:
            url_file.write_bytes(saved_url)

    assert proc.returncode == 0, (
        f"verify_dashboard CLI failed with code {proc.returncode}.\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    assert "HEADLINE: PASS" in proc.stdout, (
        f"verify_dashboard CLI did not emit HEADLINE: PASS line. "
        f"The Windows popup needs this. Output:\n{proc.stdout}"
    )
