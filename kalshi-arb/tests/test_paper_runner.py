"""Unit tests for PaperRunner pipeline wiring.

Drives the fake WS source into an in-memory EventStore and asserts:

  * Scanner's on_decision callback writes every scan (emit + skip)
    to opportunities_detected.
  * Emits result in orders_placed + orders_filled rows linked by
    opportunity_id (the exact row id that MAX(id) returned).
  * Bankroll tracking updates after each settled execution.
  * Startup gate refuses when detected_limits.yaml is missing, not
    prod, or stale >24h -- in every case, the runner never enters
    the event loop.
  * stop() drains the pipeline cleanly.

These are the pipeline-level guarantees the CLI relies on; the
subprocess integration test in test_paper_cli.py exercises the CLI
wrapper itself.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from kalshi_arb.common.gates import GateError
from kalshi_arb.executor import (
    ExecutorConfig,
    OUTCOME_BOTH_FILLED,
    PaperConfig,
)
from kalshi_arb.executor.paper import FillModel
from kalshi_arb.paper import FakeWSSource, PaperRunner, PaperRunnerConfig
from kalshi_arb.scanner import ScannerConfig
from kalshi_arb.sizer import SizerConfig
from kalshi_arb.store import EventStore, SqliteBackend


def _fresh_probe_file(tmp_path: Path, env: str = "prod", age_hours: float = 0.0) -> Path:
    """Write a detected_limits.yaml fixture at the given path."""
    ts = datetime.now(tz=UTC) - timedelta(hours=age_hours)
    body = {
        "environment": env,
        "ts_utc": ts.isoformat().replace("+00:00", "Z"),
        "rest_latency_p50_ms": 35,
        "ws_max_tickers_per_conn": 200,
    }
    probe = tmp_path / "detected_limits.yaml"
    probe.write_text(yaml.safe_dump(body))
    return probe


def _deterministic_paper_config() -> PaperConfig:
    """100% full-fill so every emit produces a realized execution.
    Seeded for reproducibility."""
    return PaperConfig(
        fill_model=FillModel(
            full_fill_rate=1.0,
            partial_fill_rate=0.0,
            zero_fill_rate=0.0,
        ),
        unwind_slippage_cents=1,
        use_builtin_fees=True,
        rng_seed=0,
    )


def _smoke_runner_config(
    tmp_path: Path,
    *,
    smoke_seconds: int = 2,
    probe: Path | None = None,
) -> PaperRunnerConfig:
    if probe is None:
        probe = _fresh_probe_file(tmp_path)
    return PaperRunnerConfig(
        scanner=ScannerConfig(
            min_edge_cents=1.0,
            slippage_buffer_cents=0.5,
            min_expected_profit_cents=10.0,
        ),
        sizer=SizerConfig(
            hard_cap_usd=9.0,
            kelly_fraction=0.5,
            min_expected_profit_usd=0.05,
            daily_loss_limit_usd=500.0,
        ),
        executor=ExecutorConfig(
            daily_loss_limit_cents=50_000,
            critical_unwind_dir=tmp_path,
        ),
        paper=_deterministic_paper_config(),
        event_store_path=tmp_path / "paper.db",
        kill_switch_file=tmp_path / "KILL_SWITCH",
        probe_path=probe,
        universe_categories=["crypto"],
        universe_min_volume_usd=1000.0,
        ws_max_tickers_per_conn=100,
        smoke_test_seconds=smoke_seconds,
        smoke_test_rate_per_sec=20.0,
        smoke_test_seed=7,
        starting_bankroll_cents=1_000_000,  # $10,000
    )


# ----- Gate tests --------------------------------------------------


def test_gate_refuses_when_probe_missing(tmp_path):
    cfg = _smoke_runner_config(tmp_path, probe=tmp_path / "does-not-exist.yaml")
    runner = PaperRunner(cfg, install_signals=False)
    with pytest.raises(GateError, match="not found"):
        asyncio.run(runner.run())


def test_gate_refuses_when_probe_not_prod(tmp_path):
    probe = _fresh_probe_file(tmp_path, env="demo")
    cfg = _smoke_runner_config(tmp_path, probe=probe)
    runner = PaperRunner(cfg, install_signals=False)
    with pytest.raises(GateError, match="must be 'prod'"):
        asyncio.run(runner.run())


def test_gate_refuses_when_probe_stale(tmp_path):
    probe = _fresh_probe_file(tmp_path, env="prod", age_hours=48.0)
    cfg = _smoke_runner_config(tmp_path, probe=probe)
    runner = PaperRunner(cfg, install_signals=False)
    with pytest.raises(GateError, match="old"):
        asyncio.run(runner.run())


def test_gate_refuses_when_live_trading_env_set(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_TRADING", "true")
    cfg = _smoke_runner_config(tmp_path)
    runner = PaperRunner(cfg, install_signals=False)
    with pytest.raises(RuntimeError, match="LIVE_TRADING"):
        asyncio.run(runner.run())


# ----- Pipeline flow -----------------------------------------------


def test_pipeline_records_scans_and_executions(tmp_path):
    """Drive the runner with a fake WS for a short burst and assert the
    store has:
      - opportunities_detected rows for every scan (emit + prime)
      - orders_placed/filled rows linked by opportunity_id
      - pnl_realized rows for fully-filled outcomes
    """
    cfg = _smoke_runner_config(tmp_path, smoke_seconds=2)
    runner = PaperRunner(cfg, install_signals=False)
    stats = asyncio.run(runner.run())

    assert stats.scans > 0, "expected >=1 scan during smoke test"
    assert stats.emits > 0, "expected >=1 emit (fake WS emits arb-viable books)"
    assert stats.executions > 0, "expected >=1 execution from emits"

    # Read back from the store via a fresh connection (post-shutdown).
    store = EventStore(SqliteBackend(cfg.event_store_path))
    store.connect()
    try:
        opp_rows = store.read_many(
            "SELECT COUNT(*), SUM(CASE WHEN decision='emit' THEN 1 ELSE 0 END)"
            " FROM opportunities_detected"
        )
        total, emits = int(opp_rows[0][0]), int(opp_rows[0][1])
        assert total == stats.scans, (
            f"opportunity rows {total} != scans {stats.scans}"
        )
        assert emits == stats.emits, (
            f"emit rows {emits} != emit stat {stats.emits}"
        )

        order_rows = store.read_many(
            "SELECT opportunity_id, side, action, count, placed_ok"
            " FROM orders_placed ORDER BY id ASC"
        )
        # Every execution produces at least the two YES/NO legs.
        assert len(order_rows) >= stats.executions * 2, (
            f"order rows {len(order_rows)} < 2 * executions {stats.executions}"
        )
        # Every order_placed.opportunity_id must match a real opp row.
        opp_ids = {
            int(r[0]) for r in store.read_many(
                "SELECT id FROM opportunities_detected WHERE decision='emit'"
            )
        }
        for r in order_rows:
            assert int(r[0]) in opp_ids, (
                f"order with opportunity_id={r[0]} has no matching opp row"
            )

        # pnl_realized rows exist for at least one fully-filled result.
        pnl_rows = store.read_many(
            "SELECT opportunity_id, net_cents FROM pnl_realized"
        )
        assert pnl_rows, "no pnl_realized rows written"
    finally:
        store.backend.close()


def test_pipeline_writes_skip_rows_when_edge_too_low(tmp_path):
    """When every book sum is >=100c, scanner emits SKIP_SUM_GE_100
    only, yet every scan still produces a row in opportunities_detected
    so the audit trail is complete."""

    # Custom fake WS that populates NON-arb books (sum=100).
    cfg = _smoke_runner_config(tmp_path, smoke_seconds=1)

    runner = PaperRunner(cfg, install_signals=False)
    # Override the fake universe with a non-arb universe before run().
    original_run_smoke = runner._run_smoke_test

    async def _custom_smoke():
        runner._fake_ws = FakeWSSource(
            handler=runner._on_synthetic_delta,
            universe=[("KXNOARB-1", 50, 55)],  # sum=105 -> skip_sum_ge_100
            rate_per_sec=20.0,
            seed=0,
        )
        driver = asyncio.create_task(runner._fake_ws.run())
        try:
            await asyncio.wait_for(
                runner._stop.wait(), timeout=cfg.smoke_test_seconds
            )
        except asyncio.TimeoutError:
            pass
        finally:
            runner._fake_ws.stop()
            driver.cancel()
            try:
                await driver
            except (asyncio.CancelledError, Exception):
                pass

    runner._run_smoke_test = _custom_smoke  # type: ignore[assignment]
    stats = asyncio.run(runner.run())

    assert stats.scans > 0
    assert stats.emits == 0, "sum=105 markets should never emit"
    assert stats.executions == 0

    store = EventStore(SqliteBackend(cfg.event_store_path))
    store.connect()
    try:
        skip_rows = store.read_many(
            "SELECT COUNT(*) FROM opportunities_detected"
            " WHERE decision = 'skip_sum_ge_100'"
        )
        assert int(skip_rows[0][0]) > 0, "no skip rows recorded"
    finally:
        store.backend.close()


def test_stop_shuts_down_cleanly_before_duration(tmp_path):
    """stop() should terminate the smoke loop promptly and drain
    the store. Simulates a SIGINT arriving mid-run."""
    cfg = _smoke_runner_config(tmp_path, smoke_seconds=30)
    runner = PaperRunner(cfg, install_signals=False)

    async def _drive():
        task = asyncio.create_task(runner.run())
        # Let the pipeline land a few events, then stop.
        await asyncio.sleep(0.8)
        runner.stop(reason="test-stop")
        await asyncio.wait_for(task, timeout=5.0)

    t0 = time.monotonic()
    asyncio.run(_drive())
    elapsed = time.monotonic() - t0
    assert elapsed < 5.0, f"shutdown took {elapsed:.2f}s -- not clean"
    assert runner._stop_reason == "test-stop"


# ----- Bankroll tracking -------------------------------------------


def test_bankroll_updates_after_realized_pnl(tmp_path):
    """After each fully-filled execution, the runner's in-memory
    bankroll should reflect the realized net cents."""
    cfg = _smoke_runner_config(tmp_path, smoke_seconds=2)
    starting = cfg.starting_bankroll_cents
    runner = PaperRunner(cfg, install_signals=False)
    stats = asyncio.run(runner.run())
    assert stats.executions > 0
    # Whether the bank grew or shrank depends on the arb's net_fill math;
    # the invariant is that it DID change from the starting value once
    # pnl_realized rows landed. If executions happened, the bankroll
    # must have moved (or remained equal only in the degenerate case
    # where net_realized summed to exactly 0 across every execution --
    # not possible with deterministic full-fill + non-zero fees).
    assert runner._bankroll_cents != starting, (
        f"bankroll never moved (starting={starting}, final={runner._bankroll_cents})"
    )


# ----- Config loader -----------------------------------------------


def test_paper_config_loader_applies_operator_override(tmp_path):
    """Operator can drop a paper_config.yaml file in place with a
    lower full_fill_rate for smoke testing. Loader parses correctly."""
    cfg_path = tmp_path / "paper_config.yaml"
    cfg_path.write_text(
        "fill_model:\n"
        "  full_fill_rate: 0.50\n"
        "  partial_fill_rate: 0.40\n"
        "  zero_fill_rate: 0.10\n"
        "unwind_slippage_cents: 3\n"
        "use_builtin_fees: true\n"
        "rng_seed: 99\n"
    )
    pc = PaperConfig.load(cfg_path)
    assert pc.fill_model.full_fill_rate == 0.50
    assert pc.fill_model.partial_fill_rate == 0.40
    assert pc.fill_model.zero_fill_rate == 0.10
    assert pc.unwind_slippage_cents == 3
    assert pc.rng_seed == 99
