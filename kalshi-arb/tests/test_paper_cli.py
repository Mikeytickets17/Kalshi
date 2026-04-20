"""Integration tests for the `kalshi-arb paper` CLI subcommand.

These tests spawn the actual CLI as a subprocess so they exercise:
  * argparse/typer wiring
  * env-var checks (LIVE_TRADING refusal)
  * prod-probe gate enforcement (missing / stale / wrong env all refuse)
  * --smoke-test lifecycle end-to-end: runs N seconds, exits cleanly
  * Event store rows visible after subprocess terminates
  * SIGINT produces a clean shutdown (not a traceback)

The subprocess subtree uses the same Python that's running the test
and inherits PYTHONPATH so the in-progress kalshi_arb package loads.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_probe(path: Path, *, env: str = "prod", age_hours: float = 0.0) -> None:
    ts = datetime.now(tz=UTC) - timedelta(hours=age_hours)
    body = {
        "environment": env,
        "ts_utc": ts.isoformat().replace("+00:00", "Z"),
        "rest_latency_p50_ms": 35,
        "ws_max_tickers_per_conn": 200,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(body))


def _run_cli(
    args: list[str],
    env: dict[str, str],
    *,
    timeout: float,
    cwd: Path,
) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "-m", "kalshi_arb.cli", *args]
    return subprocess.run(
        cmd,
        env=env,
        cwd=str(cwd),
        timeout=timeout,
        capture_output=True,
        text=True,
    )


def _base_env(db_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    # Isolate from operator's real .env: force credentials we don't have
    # to avoid accidental live or paper-api env-var refusals.
    env.pop("LIVE_TRADING", None)
    env.pop("PAPER_MODE", None)
    env["EVENT_STORE_PATH"] = str(db_path)
    env["KALSHI_API_KEY_ID"] = "test-key-id"
    env["KALSHI_PRIVATE_KEY_PATH"] = "/nonexistent-key"
    env["KALSHI_USE_DEMO"] = "true"
    # Cheaper runtime: small bankroll-adjacent knobs (doesn't matter
    # for the smoke-test path, but keeps error output predictable).
    env["HARD_CAP_USD"] = "9.0"
    env["MIN_EXPECTED_PROFIT_USD"] = "0.05"
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return env


# ---- Gate refusals -------------------------------------------------


def test_cli_refuses_without_probe_file(tmp_path):
    env = _base_env(tmp_path / "paper.db")
    proc = _run_cli(
        ["paper", "--smoke-test", "2", "--probe-path", str(tmp_path / "missing.yaml")],
        env=env,
        timeout=15,
        cwd=tmp_path,
    )
    assert proc.returncode == 3, (
        f"expected gate refusal (exit 3), got {proc.returncode}\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}"
    )
    assert "GATE REFUSED" in proc.stderr
    assert "not found" in proc.stderr


def test_cli_refuses_when_probe_not_prod(tmp_path):
    probe = tmp_path / "detected_limits.yaml"
    _write_probe(probe, env="demo")
    env = _base_env(tmp_path / "paper.db")
    proc = _run_cli(
        ["paper", "--smoke-test", "2", "--probe-path", str(probe)],
        env=env, timeout=15, cwd=tmp_path,
    )
    assert proc.returncode == 3
    assert "must be 'prod'" in proc.stderr


def test_cli_refuses_when_probe_stale(tmp_path):
    probe = tmp_path / "detected_limits.yaml"
    _write_probe(probe, env="prod", age_hours=48.0)
    env = _base_env(tmp_path / "paper.db")
    proc = _run_cli(
        ["paper", "--smoke-test", "2", "--probe-path", str(probe)],
        env=env, timeout=15, cwd=tmp_path,
    )
    assert proc.returncode == 3
    assert "old" in proc.stderr


def test_cli_refuses_when_live_trading_env_true(tmp_path):
    probe = tmp_path / "detected_limits.yaml"
    _write_probe(probe)
    env = _base_env(tmp_path / "paper.db")
    env["LIVE_TRADING"] = "true"
    proc = _run_cli(
        ["paper", "--smoke-test", "2", "--probe-path", str(probe)],
        env=env, timeout=15, cwd=tmp_path,
    )
    # RuntimeError is mapped to exit code 4 by cli.paper(). The
    # PaperKalshiAPI guard fires inside _build_pipeline (paper-api
    # constructor), which raises RuntimeError.
    assert proc.returncode != 0, (
        f"expected non-zero exit; got {proc.returncode}\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}"
    )
    assert "LIVE_TRADING" in proc.stderr


# ---- Smoke-test happy path -----------------------------------------


def test_cli_smoke_test_runs_records_events_and_exits_clean(tmp_path):
    probe = tmp_path / "detected_limits.yaml"
    _write_probe(probe)
    db = tmp_path / "paper.db"
    env = _base_env(db)
    # Use a 3-second window so the CI run is quick; 30 in production
    # per operator spec.
    duration = 3
    t0 = time.monotonic()
    proc = _run_cli(
        [
            "paper", "--smoke-test", str(duration),
            "--smoke-rate", "25", "--smoke-seed", "123",
            "--probe-path", str(probe),
        ],
        env=env, timeout=duration + 15, cwd=tmp_path,
    )
    elapsed = time.monotonic() - t0

    assert proc.returncode == 0, (
        f"smoke-test exit != 0: returncode={proc.returncode}\n"
        f"stderr={proc.stderr[-2000:]}"
    )
    # Duration +/- slack. We want to be sure it didn't exit early
    # (before smoke duration) or run massively over.
    assert duration - 0.5 < elapsed < duration + 10, (
        f"elapsed {elapsed:.1f}s outside [{duration - 0.5}, {duration + 10}]"
    )
    # Startup banner visible on stderr.
    assert "kalshi-arb paper mode starting" in proc.stderr
    assert "fill model" in proc.stderr

    # Store contents: every smoke-test run with arb-viable fake WS
    # must produce at least one opportunity row AND at least one
    # orders_placed row.
    from kalshi_arb.store import EventStore, SqliteBackend

    store = EventStore(SqliteBackend(db))
    store.connect()
    try:
        opp_total = int(
            store.read_one(
                "SELECT COUNT(*) FROM opportunities_detected"
            )[0]
        )
        emit_total = int(
            store.read_one(
                "SELECT COUNT(*) FROM opportunities_detected"
                " WHERE decision = 'emit'"
            )[0]
        )
        order_total = int(store.read_one("SELECT COUNT(*) FROM orders_placed")[0])
    finally:
        store.backend.close()

    assert opp_total > 0, "no opportunity rows recorded"
    assert emit_total > 0, (
        f"no emit rows. Total={opp_total}. "
        "Either fake WS didn't run or scanner thresholds drifted."
    )
    assert order_total >= 2, (
        f"expected >=2 orders_placed rows (YES+NO per emit); got {order_total}"
    )


# ---- SIGINT shutdown ------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="SIGINT semantics on Windows require CTRL_C_EVENT + process group.",
)
def test_cli_sigint_produces_clean_exit(tmp_path):
    """Start a longer-running smoke-test, send SIGINT, verify exit is
    clean (returncode 0, no Python traceback)."""
    probe = tmp_path / "detected_limits.yaml"
    _write_probe(probe)
    db = tmp_path / "paper.db"
    env = _base_env(db)

    proc = subprocess.Popen(
        [
            sys.executable, "-m", "kalshi_arb.cli",
            "paper", "--smoke-test", "30",
            "--smoke-rate", "20",
            "--probe-path", str(probe),
        ],
        env=env, cwd=str(tmp_path),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        # Wait for startup banner before SIGINTing so we're interrupting
        # the actual pipeline, not startup.
        time.sleep(1.5)
        proc.send_signal(signal.SIGINT)
        stdout, stderr = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate(timeout=5)
        pytest.fail(
            f"SIGINT did not produce clean shutdown within 10s.\n"
            f"stdout={stdout.decode(errors='replace')[-500:]}\n"
            f"stderr={stderr.decode(errors='replace')[-2000:]}"
        )
    assert proc.returncode == 0, (
        f"expected returncode=0 after SIGINT, got {proc.returncode}\n"
        f"stderr={stderr.decode(errors='replace')[-1500:]}"
    )
    err = stderr.decode(errors="replace")
    assert "Traceback" not in err, (
        f"SIGINT produced a traceback:\n{err[-2000:]}"
    )
    assert "shutdown_complete" in err
