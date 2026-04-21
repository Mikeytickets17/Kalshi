"""Tests for --skip-probe-gate bypass on the paper CLI.

Context: after PRs #11-#15 iterated on the prod probe, the operator
chose to bypass the gate rather than keep re-running the probe. Paper
mode uses PaperKalshiAPI (in-process, no real orders) so the probe is
defense-in-depth, not a safety requirement. This file pins the bypass
contract so we don't accidentally regress it AND so the live CLI
(future) can never silently inherit it.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kalshi_arb.paper import PaperRunner, PaperRunnerConfig
from kalshi_arb.common.gates import GateError


REPO_ROOT = Path(__file__).resolve().parents[1]


# Borrow the fixture helpers from the existing paper tests.
from tests.test_paper_runner import (
    _deterministic_paper_config,
    _fresh_probe_file,
    _smoke_runner_config,
)


def _smoke_runner_config_skip(
    tmp_path: Path, *, skip: bool = True, probe: Path | None = None
) -> PaperRunnerConfig:
    """Build a smoke-test runner config that either sets or unsets the
    bypass flag."""
    cfg = _smoke_runner_config(
        tmp_path,
        smoke_seconds=1,
        probe=probe or (tmp_path / "missing-probe.yaml"),
    )
    # _smoke_runner_config constructs the dataclass in its call site;
    # we need the bypass flag set. Easiest way: copy the fields we
    # care about into a new instance.
    return PaperRunnerConfig(
        scanner=cfg.scanner, sizer=cfg.sizer, executor=cfg.executor,
        paper=cfg.paper,
        event_store_path=cfg.event_store_path,
        kill_switch_file=cfg.kill_switch_file,
        probe_path=cfg.probe_path,
        universe_categories=cfg.universe_categories,
        universe_min_volume_usd=cfg.universe_min_volume_usd,
        ws_max_tickers_per_conn=cfg.ws_max_tickers_per_conn,
        skip_probe_gate=skip,
        smoke_test_seconds=cfg.smoke_test_seconds,
        smoke_test_rate_per_sec=cfg.smoke_test_rate_per_sec,
        smoke_test_seed=cfg.smoke_test_seed,
        starting_bankroll_cents=cfg.starting_bankroll_cents,
    )


# ---- Gate bypass: runner-level ------------------------------------


def test_runner_without_bypass_still_refuses_missing_probe(tmp_path):
    """Regression: the existing gate must keep refusing when bypass
    isn't set. This pins that --skip-probe-gate is opt-in, not the
    new default."""
    cfg = _smoke_runner_config_skip(tmp_path, skip=False)
    runner = PaperRunner(cfg, install_signals=False)
    with pytest.raises(GateError, match="not found"):
        asyncio.run(runner.run())


def test_runner_with_bypass_skips_gate_even_without_probe(tmp_path, capsys):
    """The core contract: with skip_probe_gate=True, paper runs without
    a probe file on disk. The smoke-test harness provides the pipeline;
    all we care about is that the gate doesn't raise."""
    cfg = _smoke_runner_config_skip(tmp_path, skip=True)
    runner = PaperRunner(cfg, install_signals=False)
    # Should complete (smoke_test_seconds=1).
    stats = asyncio.run(runner.run())
    # The smoke-test pipeline runs; we don't care about exact counts,
    # only that the gate didn't refuse us out of the run.
    assert stats is not None
    # The loud banner must have gone to stderr.
    captured = capsys.readouterr()
    assert "PROBE GATE BYPASSED" in captured.err
    assert "--skip-probe-gate" in captured.err
    # The banner must call out that prod calibration wasn't done.
    assert ("NOT been calibrated" in captured.err
            or "NOT calibrated" in captured.err)


def test_runner_with_bypass_still_refuses_live_trading_env(tmp_path, monkeypatch):
    """The bypass is ONLY for the probe gate, not for the LIVE_TRADING
    safety guard. Setting LIVE_TRADING=true should still refuse even
    when --skip-probe-gate is active."""
    monkeypatch.setenv("LIVE_TRADING", "true")
    cfg = _smoke_runner_config_skip(tmp_path, skip=True)
    runner = PaperRunner(cfg, install_signals=False)
    with pytest.raises(RuntimeError, match="LIVE_TRADING"):
        asyncio.run(runner.run())


def test_runner_with_bypass_and_valid_probe_still_works(tmp_path):
    """If the probe IS present and valid, --skip-probe-gate should
    not break anything. Bypass short-circuits before probe
    validation; a valid probe is simply ignored in that case."""
    probe = _fresh_probe_file(tmp_path, env="prod")
    cfg = _smoke_runner_config_skip(tmp_path, skip=True, probe=probe)
    runner = PaperRunner(cfg, install_signals=False)
    stats = asyncio.run(runner.run())
    assert stats is not None


# ---- CLI subprocess: --skip-probe-gate actually wires through -----


def _base_env(db_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("LIVE_TRADING", None)
    env.pop("PAPER_MODE", None)
    env["EVENT_STORE_PATH"] = str(db_path)
    env["KALSHI_API_KEY_ID"] = "test-key-id"
    env["KALSHI_PRIVATE_KEY_PATH"] = "/nonexistent-key"
    env["KALSHI_USE_DEMO"] = "true"
    env["HARD_CAP_USD"] = "9.0"
    env["MIN_EXPECTED_PROFIT_USD"] = "0.05"
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _run_cli(
    args: list[str], env: dict[str, str], *, timeout: float, cwd: Path
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "kalshi_arb.cli", *args],
        env=env, cwd=str(cwd), timeout=timeout,
        capture_output=True, text=True,
    )


def test_cli_paper_refuses_without_probe_by_default(tmp_path):
    """Pre-flight: without --skip-probe-gate, missing probe still refuses."""
    env = _base_env(tmp_path / "paper.db")
    proc = _run_cli(
        ["paper", "--smoke-test", "2",
         "--probe-path", str(tmp_path / "missing.yaml")],
        env=env, timeout=15, cwd=tmp_path,
    )
    assert proc.returncode == 3, (
        f"expected gate refusal, got {proc.returncode}\n"
        f"stderr={proc.stderr[-500:]}"
    )
    assert "GATE REFUSED" in proc.stderr


def test_cli_paper_with_skip_probe_gate_runs(tmp_path):
    """The happy bypass path: no probe on disk, flag set, smoke test
    runs to completion and exits cleanly."""
    env = _base_env(tmp_path / "paper.db")
    proc = _run_cli(
        ["paper", "--smoke-test", "2",
         "--skip-probe-gate",
         "--smoke-rate", "25",
         "--probe-path", str(tmp_path / "missing.yaml")],
        env=env, timeout=15, cwd=tmp_path,
    )
    assert proc.returncode == 0, (
        f"bypass run exited {proc.returncode}\n"
        f"stderr={proc.stderr[-800:]}"
    )
    # Loud banner in stderr.
    assert "PROBE GATE BYPASSED" in proc.stderr
    assert "--skip-probe-gate" in proc.stderr
    # Paper startup banner STILL appears (this is a paper run, not a
    # bypass-only exit).
    assert "kalshi-arb paper mode starting" in proc.stderr


def test_cli_paper_bypass_error_message_mentions_the_flag(tmp_path):
    """When bypass ISN'T set and the gate refuses, the error message
    points the operator to the bypass flag. Discoverability."""
    env = _base_env(tmp_path / "paper.db")
    proc = _run_cli(
        ["paper", "--smoke-test", "2",
         "--probe-path", str(tmp_path / "missing.yaml")],
        env=env, timeout=15, cwd=tmp_path,
    )
    assert proc.returncode == 3
    assert "--skip-probe-gate" in proc.stderr


def test_cli_paper_help_advertises_skip_probe_gate(tmp_path):
    """--help must mention the bypass so operators can find it."""
    env = _base_env(tmp_path / "unused.db")
    proc = _run_cli(
        ["paper", "--help"], env=env, timeout=10, cwd=tmp_path,
    )
    assert proc.returncode == 0
    assert "--skip-probe-gate" in proc.stdout
    assert "paper" in proc.stdout.lower()
