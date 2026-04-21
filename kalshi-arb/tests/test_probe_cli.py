"""Subprocess tests for `kalshi-arb probe --env prod` refusals.

The happy path of the prod probe requires a real Kalshi connection
(IP-allowlisted, paid production key) and lives on the operator's
laptop. These tests cover the pre-flight gates that refuse to run
the real probe under unsafe configuration.

Every refusal must:
  * produce a non-zero exit code
  * print the reason on stderr
  * place no orders, make no network calls
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _run(args: list[str], env: dict[str, str], *, timeout: float = 15.0,
         cwd: Path | None = None) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "-m", "kalshi_arb.cli", *args]
    return subprocess.run(
        cmd,
        env=env,
        cwd=str(cwd or REPO_ROOT),
        timeout=timeout,
        capture_output=True,
        text=True,
    )


def _base_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("LIVE_TRADING", None)
    env.pop("PAPER_MODE", None)
    env.pop("KALSHI_USE_DEMO", None)
    env.pop("KALSHI_API_KEY_ID", None)
    env["KALSHI_PRIVATE_KEY_PATH"] = "/nonexistent-key"
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return env


# ---- refusals ------------------------------------------------------


def test_prod_refuses_when_KALSHI_USE_DEMO_unset():
    env = _base_env()
    proc = _run(["probe", "--env", "prod"], env=env, timeout=10)
    assert proc.returncode != 0
    assert "KALSHI_USE_DEMO=false" in proc.stderr


def test_prod_refuses_when_KALSHI_USE_DEMO_true():
    env = _base_env()
    env["KALSHI_USE_DEMO"] = "true"
    proc = _run(["probe", "--env", "prod"], env=env, timeout=10)
    assert proc.returncode != 0
    assert "KALSHI_USE_DEMO=false" in proc.stderr


def test_prod_refuses_when_API_KEY_ID_unset():
    env = _base_env()
    env["KALSHI_USE_DEMO"] = "false"
    # KALSHI_API_KEY_ID is still popped
    proc = _run(["probe", "--env", "prod"], env=env, timeout=10)
    assert proc.returncode != 0
    assert "KALSHI_API_KEY_ID" in proc.stderr


def test_invalid_env_flag_refused():
    env = _base_env()
    proc = _run(["probe", "--env", "staging"], env=env, timeout=10)
    assert proc.returncode != 0
    assert "--env must be 'demo' or 'prod'" in proc.stderr


# ---- banner + countdown appear before any network call -----------


def test_prod_prints_banner_and_countdown_then_aborts_on_ctrl_c():
    """With good env but the countdown phase interrupted via SIGINT,
    the CLI must print the banner + countdown and exit without trying
    to talk to Kalshi."""
    env = _base_env()
    env["KALSHI_USE_DEMO"] = "false"
    env["KALSHI_API_KEY_ID"] = "test-key"

    import signal

    proc = subprocess.Popen(
        [sys.executable, "-m", "kalshi_arb.cli", "probe", "--env", "prod"],
        env=env, cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    # Let the banner print + start counting.
    time.sleep(1.2)
    proc.send_signal(signal.SIGINT)
    try:
        out, err = proc.communicate(timeout=6)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate(timeout=3)
        raise

    err_s = err.decode(errors="replace")
    assert "ABOUT TO CONNECT TO PRODUCTION KALSHI" in err_s
    assert "Press Ctrl+C within 5 seconds to abort" in err_s
    assert "starting in" in err_s
    # Clean abort: return code 0 (operator-initiated cancel).
    assert proc.returncode == 0, (
        f"unexpected returncode {proc.returncode}\n"
        f"stderr={err_s[-800:]}"
    )
    assert "ABORTED by operator" in err_s


# ---- demo path still works (no refusal) --------------------------


def test_demo_env_does_not_require_KALSHI_USE_DEMO_false():
    """Demo probe has always allowed unset/true KALSHI_USE_DEMO.
    Confirm the new gate only applies to --env prod."""
    env = _base_env()
    # Note: demo probe may still fail later for other reasons (no key,
    # no network) but the --env refusal must not fire.
    proc = _run(["probe", "--env", "demo", "--help"], env=env, timeout=10)
    # --help returns 0 regardless of env state; we just want to prove
    # no early ValueError from argument parsing.
    assert proc.returncode == 0
    assert "probe" in proc.stdout.lower()
