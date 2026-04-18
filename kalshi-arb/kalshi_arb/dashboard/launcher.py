"""Dashboard launcher: uvicorn + cloudflared quick tunnel.

Paper-phase deployment. Zero cloud signup. Zero credit card. Runs on
the operator's laptop; when laptop is off, everything is off. When we
get to live trading we revisit -- see docs/live-migration.md.

What this file does
-------------------
1. Ensure cloudflared is on disk (auto-download on first run).
2. Ensure the dashboard password exists (auto-generate + stash in
   .dashboard_creds on first run; read thereafter).
3. Spawn uvicorn as a subprocess serving the FastAPI dashboard on
   127.0.0.1:<PORT>.
4. Spawn cloudflared as a subprocess in quick-tunnel mode, pointing
   at the local uvicorn. Quick tunnels require no Cloudflare account.
5. Parse cloudflared's output for the generated *.trycloudflare.com URL.
   Print it loudly. Write it to dashboard_url.txt in the repo root.
6. Stream both children's output. On Ctrl+C, terminate both cleanly.

Design notes
------------
* cloudflared writes its progress logs to stdout+stderr merged. We pipe
  them and drain in a daemon thread so the OS pipe buffer never fills.
* The URL capture is one-shot: once seen, we stop scanning but keep
  draining (otherwise the subprocess would block on its next write).
* Child-process signal handling: Popen.terminate() is SIGTERM on Unix,
  TerminateProcess on Windows -- both sufficient for our two processes
  which handle shutdown gracefully.
"""

from __future__ import annotations

import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable


# The quick-tunnel URL pattern cloudflared prints once the tunnel is live.
URL_PATTERN = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")

# Release URLs for cloudflared. Pinning to /latest keeps the binary up
# to date without us shipping a broken old version on a stale repo.
CLOUDFLARED_URLS = {
    "win32": "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe",
    "linux": "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64",
    "darwin": "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-amd64.tgz",
}


def _repo_root() -> Path:
    """Repo root = the kalshi-arb/ directory (where pyproject.toml lives)."""
    # __file__ = kalshi-arb/kalshi_arb/dashboard/launcher.py
    # parents[0] = dashboard, [1] = kalshi_arb, [2] = kalshi-arb
    return Path(__file__).resolve().parents[2]


def ensure_cloudflared(bin_dir: Path) -> Path:
    """Return a path to an executable cloudflared. Download if missing.

    The binary is ~25 MB and only needed at runtime, so we don't commit
    it to git -- we keep it under bin/ which is .gitignored.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    is_win = sys.platform == "win32"
    bin_name = "cloudflared.exe" if is_win else "cloudflared"
    path = bin_dir / bin_name

    if path.exists():
        return path

    plat = "win32" if is_win else ("darwin" if sys.platform == "darwin" else "linux")
    url = CLOUDFLARED_URLS[plat]
    print(f"[launcher] downloading cloudflared ({plat}) from {url}")
    print("[launcher] this is ~25 MB and only happens once.")

    tmp = path.with_suffix(path.suffix + ".partial")
    try:
        urllib.request.urlretrieve(url, tmp)
    except Exception as exc:
        raise RuntimeError(
            f"cloudflared download failed: {exc}. Check internet, then rerun."
        ) from exc

    tmp.replace(path)
    if not is_win:
        path.chmod(0o755)
    print(f"[launcher] cloudflared saved to {path}")
    return path


def ensure_password(creds_path: Path) -> str:
    """Read or generate the dashboard password.

    First-run flow:
      - generate a URL-safe 20-byte token (~27 chars)
      - save to .dashboard_creds (chmod 600 on Unix)
      - print to terminal so the operator can note it
    Subsequent runs:
      - just read the file
    """
    if creds_path.exists():
        pw = creds_path.read_text(encoding="utf-8").strip()
        if pw:
            return pw
    pw = secrets.token_urlsafe(20)
    creds_path.write_text(pw, encoding="utf-8")
    try:
        creds_path.chmod(0o600)
    except Exception:
        pass
    print()
    print("=" * 70)
    print("  FIRST RUN -- dashboard password was auto-generated.")
    print(f"  Password:  {pw}")
    print(f"  Saved to:  {creds_path}  (gitignored)")
    print("  Username:  admin")
    print("  Write the password down somewhere you trust.")
    print("=" * 70)
    print()
    return pw


def _spawn_uvicorn(port: int, env: dict[str, str], cwd: Path) -> subprocess.Popen:
    return subprocess.Popen(
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
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        text=True,
    )


def _spawn_cloudflared(cloudflared: Path, port: int) -> subprocess.Popen:
    return subprocess.Popen(
        [str(cloudflared), "tunnel", "--url", f"http://127.0.0.1:{port}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        text=True,
    )


def _drain_until_exit(
    proc: subprocess.Popen,
    *,
    tag: str,
    on_url: Callable[[str], None] | None = None,
) -> None:
    """Stream a child process's output; on the first trycloudflare.com
    URL found, invoke on_url(url) exactly once then keep draining."""
    url_seen = False
    if proc.stdout is None:
        return
    for line in iter(proc.stdout.readline, ""):
        if line:
            sys.stdout.write(f"[{tag}] {line}")
            sys.stdout.flush()
            if not url_seen and on_url is not None:
                m = URL_PATTERN.search(line)
                if m:
                    url_seen = True
                    try:
                        on_url(m.group(0))
                    except Exception as exc:  # noqa: BLE001
                        print(f"[launcher] on_url callback raised: {exc}")


def run(port: int | None = None, wait_for_url_sec: float = 30.0) -> int:
    repo = _repo_root()
    bin_dir = repo / "bin"
    creds_path = repo / ".dashboard_creds"
    url_file = repo / "dashboard_url.txt"

    actual_port = port if port is not None else int(os.environ.get("DASHBOARD_PORT", "8000"))

    # 1) cloudflared binary
    cloudflared = ensure_cloudflared(bin_dir)
    # 2) password
    password = ensure_password(creds_path)

    # Build the environment uvicorn will see.
    env = os.environ.copy()
    env["DASHBOARD_PASSWORD"] = password
    env["PORT"] = str(actual_port)
    env.setdefault("DASHBOARD_USERNAME", "admin")
    env.setdefault(
        "DASHBOARD_SESSION_SECRET",
        # Random per-launch unless operator pinned one. Cookies reset on
        # each relaunch which is fine for single-operator paper mode.
        secrets.token_urlsafe(48),
    )

    print(f"[launcher] starting uvicorn on 127.0.0.1:{actual_port}")
    uvi = _spawn_uvicorn(actual_port, env, cwd=repo)

    # Give uvicorn a moment to bind the port before cloudflared connects.
    time.sleep(2.0)
    if uvi.poll() is not None:
        print(f"[launcher] uvicorn exited before cloudflared spawn; code={uvi.returncode}")
        return 1

    print("[launcher] starting cloudflared quick tunnel (no account required)")
    cf = _spawn_cloudflared(cloudflared, actual_port)

    # Shared state between threads.
    url_container: dict[str, str] = {}
    url_event = threading.Event()

    def _on_url(u: str) -> None:
        url_container["url"] = u
        try:
            url_file.write_text(u, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            print(f"[launcher] failed to write {url_file}: {exc}")
        banner = "=" * 70
        print()
        print(banner)
        print(f"  DASHBOARD URL:  {u}")
        print("  LOGIN:          admin / <see .dashboard_creds>")
        print(f"  URL FILE:       {url_file}")
        print(banner)
        print()
        url_event.set()

    cf_drain = threading.Thread(
        target=_drain_until_exit, args=(cf,), kwargs={"tag": "cloudflared", "on_url": _on_url}, daemon=True
    )
    uvi_drain = threading.Thread(
        target=_drain_until_exit, args=(uvi,), kwargs={"tag": "uvicorn"}, daemon=True
    )
    cf_drain.start()
    uvi_drain.start()

    deadline = time.monotonic() + wait_for_url_sec
    while not url_event.is_set() and time.monotonic() < deadline:
        if cf.poll() is not None:
            print(f"[launcher] cloudflared exited before URL capture; code={cf.returncode}")
            _terminate(uvi, cf)
            return 2
        if uvi.poll() is not None:
            print(f"[launcher] uvicorn died before URL capture; code={uvi.returncode}")
            _terminate(uvi, cf)
            return 3
        time.sleep(0.25)

    if not url_event.is_set():
        print(f"[launcher] no tunnel URL seen after {wait_for_url_sec}s -- giving up")
        _terminate(uvi, cf)
        return 4

    # Loop until either process dies OR the operator Ctrl+Cs.
    try:
        while True:
            if cf.poll() is not None:
                print(f"[launcher] cloudflared exited; code={cf.returncode}")
                break
            if uvi.poll() is not None:
                print(f"[launcher] uvicorn exited; code={uvi.returncode}")
                break
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\n[launcher] Ctrl+C -- shutting down both children")
    finally:
        _terminate(uvi, cf)
    return 0


def _terminate(*procs: subprocess.Popen) -> None:
    for p in procs:
        if p is None or p.poll() is not None:
            continue
        try:
            p.terminate()
        except Exception:
            pass
    deadline = time.monotonic() + 5.0
    for p in procs:
        if p is None:
            continue
        remaining = max(0.0, deadline - time.monotonic())
        try:
            p.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            try:
                p.kill()
            except Exception:
                pass


if __name__ == "__main__":
    # When invoked as `python -m kalshi_arb.dashboard.launcher`.
    sys.exit(run())
