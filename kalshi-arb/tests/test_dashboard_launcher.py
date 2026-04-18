"""Dashboard launcher tests.

Two layers:

1. Unit tests (always run). They inject a *fake* cloudflared that emits
   a synthesized tunnel URL on its stdout, then exits. The launcher's
   URL-capture path is exercised; nothing touches the real Cloudflare
   network.

2. Live integration test (marked 'live', skipped by default). Spawns
   the real launcher which downloads cloudflared and opens a real
   quick tunnel. Asserts the banner URL responds 200 on /healthz
   within 30 s. Run with `pytest -m live` when you want to verify the
   full end-to-end path manually.

The unit tests monkey-patch the launcher's _spawn_cloudflared and
ensure_cloudflared helpers so no network access occurs. That keeps the
suite deterministic and CI-safe while still proving the launcher's
shutdown / termination / URL capture logic is correct.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from kalshi_arb.dashboard import launcher as lm


# ----------------------------------------------------------------------
# Fake cloudflared helpers.
# ----------------------------------------------------------------------


FAKE_CF_SCRIPT = textwrap.dedent(
    """
    import sys, time
    # Cloudflared-style banner the launcher grep's for.
    time.sleep(0.2)
    print('2026-04-18T00:00:00Z INF Your quick Tunnel has been created:')
    print('2026-04-18T00:00:00Z INF https://fake-tunnel-abc123.trycloudflare.com')
    sys.stdout.flush()
    # Hold the connection for a bit so the launcher can capture + log.
    time.sleep(3.0)
    """
).strip()


def _write_fake_cloudflared(tmp_path: Path) -> Path:
    """Create a python script that masquerades as cloudflared."""
    fake = tmp_path / "fake_cloudflared.py"
    fake.write_text(FAKE_CF_SCRIPT, encoding="utf-8")
    return fake


# ----------------------------------------------------------------------
# Unit tests.
# ----------------------------------------------------------------------


def test_url_pattern_matches_real_cloudflared_banner() -> None:
    """Regression guard: the pattern must match the format cloudflared
    actually emits. If cloudflared ever changes this format we'll see
    the live test fail and this unit test will help us re-derive it."""
    samples = [
        "2026-04-18T00:00:00Z INF https://fluffy-cat-1234.trycloudflare.com",
        "INF Your quick tunnel is available at https://a-b-c-1.trycloudflare.com ",
        "https://x.trycloudflare.com",
    ]
    for s in samples:
        m = lm.URL_PATTERN.search(s)
        assert m is not None, f"pattern failed to match: {s!r}"
        assert m.group(0).endswith(".trycloudflare.com")


def test_ensure_password_generates_on_first_run(tmp_path: Path) -> None:
    creds = tmp_path / ".dashboard_creds"
    assert not creds.exists()
    pw = lm.ensure_password(creds)
    assert len(pw) >= 20
    assert creds.exists()
    assert creds.read_text(encoding="utf-8").strip() == pw


def test_ensure_password_reuses_existing(tmp_path: Path) -> None:
    creds = tmp_path / ".dashboard_creds"
    creds.write_text("my-predetermined-password-abc123", encoding="utf-8")
    pw = lm.ensure_password(creds)
    assert pw == "my-predetermined-password-abc123"


def test_ensure_cloudflared_returns_existing_without_download(tmp_path: Path) -> None:
    """If a cloudflared binary is already present in bin/, the launcher
    must NOT re-download (that would eat bandwidth on every run)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    name = "cloudflared.exe" if sys.platform == "win32" else "cloudflared"
    existing = bin_dir / name
    existing.write_bytes(b"#!/bin/sh\nexit 0\n")
    path = lm.ensure_cloudflared(bin_dir)
    assert path == existing
    # Verify file bytes are unchanged (not overwritten by a download).
    assert existing.read_bytes() == b"#!/bin/sh\nexit 0\n"


def test_launcher_captures_url_from_fake_cloudflared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end inside the launcher, minus the real tunnel:
      - stub cloudflared with a script that prints a fake URL on stdout
      - stub uvicorn with `python -m http.server` on a free port
      - start_dashboard... verifies the URL is captured and written
        to dashboard_url.txt.

    This exercises the threading, the URL parse, the file write, and
    the termination path without needing internet.
    """
    fake_cf = _write_fake_cloudflared(tmp_path)

    # Point the launcher at a tmp repo root so it doesn't touch the real
    # .dashboard_creds / dashboard_url.txt.
    monkeypatch.setattr(lm, "_repo_root", lambda: tmp_path)

    # Stub cloudflared download / spawn.
    def _fake_ensure_cloudflared(bin_dir: Path) -> Path:
        bin_dir.mkdir(parents=True, exist_ok=True)
        return fake_cf

    def _fake_spawn_cloudflared(cloudflared: Path, port: int) -> subprocess.Popen:
        return subprocess.Popen(
            [sys.executable, str(cloudflared)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            text=True,
        )

    monkeypatch.setattr(lm, "ensure_cloudflared", _fake_ensure_cloudflared)
    monkeypatch.setattr(lm, "_spawn_cloudflared", _fake_spawn_cloudflared)

    # Stub uvicorn with a trivially-exiting process. The launcher waits
    # for BOTH processes; our fake cloudflared exits after ~3s which
    # triggers the main loop's exit path, so we need a uvicorn stub that
    # stays alive at least that long. `python -c sleep` is enough.
    def _fake_spawn_uvicorn(port: int, env: dict, cwd: Path) -> subprocess.Popen:
        return subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            text=True,
        )

    monkeypatch.setattr(lm, "_spawn_uvicorn", _fake_spawn_uvicorn)

    # Run the launcher in a thread; it blocks until both children exit.
    url_file = tmp_path / "dashboard_url.txt"
    exit_code = {"code": None}

    def _run():
        exit_code["code"] = lm.run(port=8765, wait_for_url_sec=5.0)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=15.0)
    assert not t.is_alive(), "launcher didn't exit within 15 s"

    assert url_file.exists(), "launcher did not write dashboard_url.txt"
    contents = url_file.read_text(encoding="utf-8").strip()
    assert contents == "https://fake-tunnel-abc123.trycloudflare.com", (
        f"unexpected URL captured: {contents!r}"
    )
    # Exit code 0 = happy; fake cloudflared exited cleanly after capture.
    assert exit_code["code"] == 0


def test_launcher_times_out_when_no_url_appears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If cloudflared never emits a URL (broken network, DNS block, etc.)
    the launcher must give up after wait_for_url_sec and terminate both
    children cleanly -- not hang forever."""
    silent_cf = tmp_path / "silent_cf.py"
    silent_cf.write_text("import time; time.sleep(30)", encoding="utf-8")

    monkeypatch.setattr(lm, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(lm, "ensure_cloudflared", lambda _bin: silent_cf)
    monkeypatch.setattr(
        lm,
        "_spawn_cloudflared",
        lambda cf, port: subprocess.Popen(
            [sys.executable, str(cf)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            text=True,
        ),
    )
    monkeypatch.setattr(
        lm,
        "_spawn_uvicorn",
        lambda port, env, cwd: subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            text=True,
        ),
    )

    start = time.monotonic()
    code = lm.run(port=8766, wait_for_url_sec=2.0)
    elapsed = time.monotonic() - start
    assert code == 4, f"expected exit code 4 (timeout), got {code}"
    # Must return within a small margin of the wait_for_url_sec deadline
    # plus the 5 s termination grace.
    assert elapsed < 10.0, f"launcher took {elapsed:.1f}s -- hung past timeout"


# ----------------------------------------------------------------------
# Live integration test -- run with `pytest -m live`.
# ----------------------------------------------------------------------


@pytest.mark.live
def test_launcher_end_to_end_real_tunnel(tmp_path: Path) -> None:
    """Runs the real launcher. Requires:
      - outbound network to github.com (cloudflared download) and to
        *.trycloudflare.com (tunnel)
      - the kalshi-arb package importable from tmp_path as cwd

    Not part of the default suite -- it hits the public network. Run
    explicitly with `pytest -m live tests/test_dashboard_launcher.py` when
    you want to verify the full path works on a real machine before
    gate check.
    """
    import http.client

    # Start the launcher in its own process so we can Ctrl+C it cleanly.
    proc = subprocess.Popen(
        [sys.executable, "-m", "kalshi_arb.dashboard.launcher"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        text=True,
        env={**os.environ, "DASHBOARD_PORT": "8767"},
    )
    url_file = lm._repo_root() / "dashboard_url.txt"

    try:
        deadline = time.monotonic() + 45.0
        while time.monotonic() < deadline:
            if url_file.exists():
                url = url_file.read_text(encoding="utf-8").strip()
                if url.startswith("https://"):
                    break
            time.sleep(1.0)
        else:
            pytest.fail("no tunnel URL after 45 s")

        # Resolve + hit /healthz. Use http.client to avoid adding deps.
        from urllib.parse import urlparse
        u = urlparse(url + "/healthz")
        conn = http.client.HTTPSConnection(u.hostname, timeout=15)
        conn.request("GET", u.path)
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
        assert resp.status == 200, f"healthz returned {resp.status}: {body}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
