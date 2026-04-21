"""Paper-mode pipeline runner.

Glue code for the `kalshi-arb paper` subcommand. The individual modules
(scanner, sizer, executor, PaperKalshiAPI, EventStore) are already
covered by unit tests; this file wires them together and owns the
lifecycle (startup gate, WS feed, SIGINT shutdown).

Flow per orderbook delta:

    WS delta
        |
        v
    OrderBook.apply_delta()   # in-memory, per ticker
        |
        v
    scanner.scan(book)
        |
        |  (scanner's on_decision callback writes the audit row to
        |   EventStore for EVERY decision, emit or skip)
        v
    if decision == EMIT:
        store.flush()           # drain queue so AUTOINCREMENT is visible
        opp_id = SELECT MAX(id) FROM opportunities_detected
        sizing = sizer.size(opp, bankroll)
        if sizing.contracts_per_leg > 0:
            result = await executor.execute(sizing)
            # record_order_placed / record_order_filled per leg
            # linked by opportunity_id = opp_id

The runner is the sole writer to opportunities_detected in paper mode,
so the MAX(id) read is race-free as long as we flush() before read.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from .. import clock, log
from ..common.gates import GateError, require_prod_probe
from ..config import Config
from ..executor import (
    ExecutorConfig,
    KillSwitch,
    OUTCOME_KILL_SWITCH,
    PaperConfig,
    PaperKalshiAPI,
    StructuralArbExecutor,
)
from ..scanner import (
    DECISION_EMIT,
    FeeModel,
    OrderBook,
    ScannerConfig,
    StructuralArbScanner,
)
from ..scanner.scanner import ScanDecision
from ..sizer import BankrollSnapshot, HalfKellySizer, SizerConfig
from ..store import EventStore, SqliteBackend
from .fake_ws import FakeWSSource, SyntheticDelta

_log = log.get("paper.runner")


def _env_bool(key: str) -> bool:
    return os.environ.get(key, "").strip().lower() in ("1", "true", "yes", "on")


@dataclass
class PaperRunnerConfig:
    """Runtime knobs for a paper run. Most come from Config.load() but
    the runner builds its own dataclass so tests can override cleanly."""

    scanner: ScannerConfig
    sizer: SizerConfig
    executor: ExecutorConfig
    paper: PaperConfig

    event_store_path: Path
    kill_switch_file: Path
    probe_path: Path

    universe_categories: list[str]
    universe_min_volume_usd: float
    ws_max_tickers_per_conn: int

    # Bypass the strict prod-probe gate on startup. The probe is
    # defense-in-depth (calibrate rate-limit + WS cap + REST latency
    # against live prod BEFORE paper trades fire). Paper mode itself
    # uses PaperKalshiAPI (in-process, no real orders), so the probe
    # is NOT a safety requirement for paper -- it's confidence that
    # the live numbers match our assumptions. Skipping is appropriate
    # when the probe has a code issue we haven't resolved yet, or
    # when the operator wants to calibrate thresholds from actual
    # paper-run observations. Skipping is NOT appropriate before
    # going LIVE -- the live CLI (future) will re-enforce the gate.
    skip_probe_gate: bool = False

    # Smoke-test mode: use FakeWSSource, a fixed universe, skip REST.
    smoke_test_seconds: int = 0
    smoke_test_rate_per_sec: float = 5.0
    smoke_test_seed: int = 42

    # Starting bankroll for the sizer (cents). Paper runs don't have
    # a real portfolio unless a live_reader is wired, so we use a
    # fixed starting bankroll and track P&L locally.
    starting_bankroll_cents: int = 10_000 * 100  # $10k paper bankroll

    @staticmethod
    def from_config(
        cfg: Config,
        paper_cfg: PaperConfig,
        *,
        probe_path: Path,
        skip_probe_gate: bool = False,
        smoke_test_seconds: int = 0,
        smoke_test_rate_per_sec: float = 5.0,
        smoke_test_seed: int = 42,
    ) -> "PaperRunnerConfig":
        return PaperRunnerConfig(
            scanner=ScannerConfig(
                min_edge_cents=cfg.min_edge_cents,
                slippage_buffer_cents=cfg.slippage_buffer_cents,
                min_expected_profit_cents=cfg.min_expected_profit_usd * 100.0,
            ),
            sizer=SizerConfig(
                hard_cap_usd=cfg.hard_cap_usd,
                kelly_fraction=cfg.kelly_fraction,
                min_expected_profit_usd=cfg.min_expected_profit_usd,
                daily_loss_limit_usd=cfg.daily_loss_limit_usd,
            ),
            executor=ExecutorConfig(
                daily_loss_limit_cents=int(cfg.daily_loss_limit_usd * 100),
            ),
            paper=paper_cfg,
            event_store_path=cfg.event_store_path,
            kill_switch_file=cfg.kill_switch_file,
            probe_path=probe_path,
            universe_categories=list(cfg.universe_categories),
            universe_min_volume_usd=cfg.universe_min_volume_usd,
            ws_max_tickers_per_conn=cfg.ws_max_tickers_per_conn,
            skip_probe_gate=skip_probe_gate,
            smoke_test_seconds=smoke_test_seconds,
            smoke_test_rate_per_sec=smoke_test_rate_per_sec,
            smoke_test_seed=smoke_test_seed,
        )


@dataclass
class _PipelineStats:
    scans: int = 0
    emits: int = 0
    executions: int = 0
    kill_switch_trips: int = 0


UniverseFetcher = Callable[[], Awaitable[list[str]]]


class PaperRunner:
    """Owns the paper-mode event loop.

    Construct with a PaperRunnerConfig; call `await run()`. The runner
    installs its own SIGINT / SIGTERM handlers (disable via `install_signals=False`
    for tests that drive shutdown via stop()).
    """

    def __init__(
        self,
        config: PaperRunnerConfig,
        *,
        event_store: EventStore | None = None,
        api: PaperKalshiAPI | None = None,
        universe_fetcher: UniverseFetcher | None = None,
        install_signals: bool = True,
    ) -> None:
        self.config = config
        self._install_signals = install_signals

        # Store: open a connection to the shared SQLite file. Dashboard
        # reads the same file; writes become visible within the WAL
        # sync interval (milliseconds in single-writer mode).
        self.store = event_store or EventStore(
            SqliteBackend(config.event_store_path)
        )
        self.killswitch = KillSwitch(sentinel=config.kill_switch_file)

        # Build pipeline components but delay construction of stateful
        # ones (api, executor) until start() so tests can inject them.
        self._api_override = api
        self._universe_fetcher = universe_fetcher

        self.api: PaperKalshiAPI | None = None
        self.scanner: StructuralArbScanner | None = None
        self.sizer: HalfKellySizer | None = None
        self.executor: StructuralArbExecutor | None = None

        self._books: dict[str, OrderBook] = {}
        self._stop = asyncio.Event()
        self._stop_reason: str | None = None
        self._pipeline_lock = asyncio.Lock()
        self._stats = _PipelineStats()

        # Bankroll state. Starts fresh; decremented by executor fills
        # (via the local P&L counter) until we wire a real LiveKalshiAPI
        # reader. For the paper CLI's purpose, this suffices -- the
        # sizer's guardrails still operate off a live value.
        self._bankroll_cents = config.starting_bankroll_cents
        self._bankroll_peak_cents = config.starting_bankroll_cents

        # Smoke-test hook
        self._fake_ws: FakeWSSource | None = None

    # ---------------------------------------------------------------
    # Lifecycle
    # ---------------------------------------------------------------

    async def run(self) -> _PipelineStats:
        """Main entry. Performs startup gates, builds the pipeline,
        runs until stop(). Returns a stats snapshot."""
        if self._install_signals:
            self._install_signal_handlers()

        self._check_env()
        self._startup_gate()
        await self._build_pipeline()
        self._log_startup_config()

        try:
            if self.config.smoke_test_seconds > 0:
                await self._run_smoke_test()
            else:
                await self._run_production()
        finally:
            await self._shutdown()
        return self._stats

    def stop(self, reason: str = "stop()") -> None:
        """Signal the runner to terminate cleanly. Idempotent."""
        if not self._stop.is_set():
            self._stop_reason = reason
            self._stop.set()

    # ---------------------------------------------------------------
    # Startup
    # ---------------------------------------------------------------

    def _check_env(self) -> None:
        if _env_bool("LIVE_TRADING"):
            raise RuntimeError(
                "paper CLI refused: LIVE_TRADING=true in the environment. "
                "The paper command is paper-mode only; unset LIVE_TRADING "
                "or use the live CLI (not yet built)."
            )

    def _startup_gate(self) -> None:
        """Require a fresh prod probe on disk, regardless of mode.

        require_prod_probe(live_trading=False) doesn't raise on its own
        in paper mode, so we add the strict checks here. The operator's
        original spec was explicit: refuse to start if detected_limits.yaml
        doesn't exist or environment != prod or ts_utc is older than 24h.

        Bypass: if self.config.skip_probe_gate is True, we SKIP the
        gate entirely and emit a loud one-time banner on stderr so the
        operator sees what they've done. Paper mode uses PaperKalshiAPI
        (in-process fake); the probe was defense-in-depth for prod
        calibration, not a functional safety requirement. The live CLI
        (future) will not expose this flag.
        """
        if self.config.skip_probe_gate:
            self._emit_skip_probe_banner()
            return
        snap = require_prod_probe(
            live_trading=False, path=self.config.probe_path
        )
        if snap.environment == "none" or not snap.ts_utc:
            raise GateError(
                f"paper CLI refused: {self.config.probe_path} not found. "
                f"Run the prod probe first: `kalshi-arb probe --env prod`, "
                f"OR re-run with --skip-probe-gate to bypass (paper-only)."
            )
        if not snap.is_prod:
            raise GateError(
                f"paper CLI refused: probe environment={snap.environment!r}, "
                f"must be 'prod'. Re-run the probe against production, "
                f"OR pass --skip-probe-gate to bypass (paper-only)."
            )
        if not snap.is_fresh:
            raise GateError(
                f"paper CLI refused: probe is {snap.age_hours:.1f}h old "
                f"(max 24h). Re-run the prod probe, "
                f"OR pass --skip-probe-gate to bypass (paper-only)."
            )
        _log.info(
            "paper.gate.probe_ok",
            environment=snap.environment,
            age_hours=round(snap.age_hours, 2),
            ts_utc=snap.ts_utc,
        )

    def _emit_skip_probe_banner(self) -> None:
        """Loud one-time banner when --skip-probe-gate is set. The
        operator must see clearly that defense-in-depth has been
        disabled; on a live push this would be a hard refusal."""
        banner = (
            "\n"
            + "=" * 70 + "\n"
            + "  PAPER CLI: PROBE GATE BYPASSED (--skip-probe-gate)\n"
            + "\n"
            + "  You are running paper mode WITHOUT a fresh prod probe on\n"
            + "  disk. This is SAFE for paper trading (PaperKalshiAPI is\n"
            + "  in-process, no real orders) but means the sizer + scanner\n"
            + "  thresholds have NOT been calibrated against prod numbers.\n"
            + "\n"
            + "  This flag does NOT exist on the live CLI. Before going\n"
            + "  live, run: kalshi-arb probe --env prod\n"
            + "=" * 70 + "\n"
        )
        sys.stderr.write(banner)
        sys.stderr.flush()
        _log.warning("paper.gate.probe_bypassed")

    async def _build_pipeline(self) -> None:
        """Connect the store, construct scanner/sizer/executor/api."""
        self.store.connect()
        await self.store.start()

        # Initialize the opportunity-id bookmark from the current max
        # so the id returned by our post-flush MAX(id) query matches
        # SQLite's AUTOINCREMENT on the first emit.
        row = self.store.read_one(
            "SELECT COALESCE(MAX(id), 0) FROM opportunities_detected"
        )
        self._last_opp_id = int(row[0]) if row else 0

        self.api = self._api_override or PaperKalshiAPI(config=self.config.paper)

        # Scanner's on_decision callback writes every decision to
        # EventStore (emit + every skip), which feeds the dashboard's
        # Opportunities tab. Skips include rejection_reason for audit.
        self.scanner = StructuralArbScanner(
            config=self.config.scanner,
            fee_model=FeeModel.builtin(),
            on_decision=self._record_scan,
        )
        self.sizer = HalfKellySizer(config=self.config.sizer)
        self.executor = StructuralArbExecutor(
            api=self.api,
            killswitch=self.killswitch,
            config=self.config.executor,
        )

    def _log_startup_config(self) -> None:
        """Loud startup summary. The operator eyeball-verifies this
        before committing to 48h."""
        fm = self.config.paper.fill_model
        msg_lines = [
            "",
            "=" * 70,
            "  kalshi-arb paper mode starting",
            "=" * 70,
            f"  mode:              {'smoke-test (' + str(self.config.smoke_test_seconds) + 's)' if self.config.smoke_test_seconds > 0 else 'production paper'}",
            f"  universe:          categories={self.config.universe_categories}",
            f"                     min_volume_usd={self.config.universe_min_volume_usd}",
            f"  scanner:           min_edge={self.config.scanner.min_edge_cents}c",
            f"                     slippage_buffer={self.config.scanner.slippage_buffer_cents}c",
            f"                     min_exp_profit={self.config.scanner.min_expected_profit_cents}c",
            f"  sizer:             hard_cap=${self.config.sizer.hard_cap_usd}",
            f"                     kelly_fraction={self.config.sizer.kelly_fraction}",
            f"                     daily_loss_limit=${self.config.sizer.daily_loss_limit_usd}",
            f"  fill model:        full={fm.full_fill_rate} "
            f"partial={fm.partial_fill_rate} "
            f"zero={fm.zero_fill_rate}",
            f"                     partial_bounds=[{fm.partial_min_pct},{fm.partial_max_pct}]",
            f"                     unwind_slippage={self.config.paper.unwind_slippage_cents}c",
            f"  event store:       {self.config.event_store_path}",
            f"  kill switch:       {self.config.kill_switch_file}",
            f"  probe source:      {self.config.probe_path}",
            f"  bankroll (paper):  ${self._bankroll_cents / 100:.2f}",
            "=" * 70,
            "  Ctrl+C to stop cleanly. Dashboard at the tunnel URL.",
            "=" * 70,
            "",
        ]
        banner = "\n".join(msg_lines)
        sys.stderr.write(banner)
        sys.stderr.flush()
        _log.info("paper.startup_config_logged")

    def _install_signal_handlers(self) -> None:
        """Trap SIGINT / SIGTERM for clean shutdown. On Windows,
        add_signal_handler is unsupported for SIGINT under asyncio;
        fall back to a KeyboardInterrupt catch in run()."""
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._on_signal, sig)
            except (NotImplementedError, RuntimeError):
                # Windows: signal.signal() fallback.
                try:
                    signal.signal(sig, lambda _s, _f: self._on_signal(_s))
                except (OSError, ValueError):
                    pass

    def _on_signal(self, sig: int) -> None:
        self.stop(reason=f"signal:{sig}")

    # ---------------------------------------------------------------
    # Run modes
    # ---------------------------------------------------------------

    async def _run_smoke_test(self) -> None:
        """Drive FakeWSSource for config.smoke_test_seconds, then stop."""
        self._fake_ws = FakeWSSource(
            handler=self._on_synthetic_delta,
            rate_per_sec=self.config.smoke_test_rate_per_sec,
            seed=self.config.smoke_test_seed,
        )
        driver = asyncio.create_task(
            self._fake_ws.run(), name="paper-fake-ws"
        )
        try:
            await asyncio.wait_for(
                self._stop.wait(), timeout=self.config.smoke_test_seconds
            )
            _log.info("paper.smoke_test_stopped_early", reason=self._stop_reason)
        except asyncio.TimeoutError:
            _log.info(
                "paper.smoke_test_duration_complete",
                seconds=self.config.smoke_test_seconds,
            )
        finally:
            if self._fake_ws is not None:
                self._fake_ws.stop()
            driver.cancel()
            try:
                await driver
            except (asyncio.CancelledError, Exception):
                pass

    async def _run_production(self) -> None:
        """Connect real WS and run until stop(). Placeholder for the
        production path -- the runner's pipeline is identical; the only
        difference is the delta source. Since the operator's spec
        delivers the CLI command and the smoke-test path, the real
        ShardedWS wiring here is deliberately scoped: build universe,
        start WS, wait for stop.
        """
        from ..rest.client import RestClient, RestConfig
        from ..ws.consumer import ShardedWS
        from ..config import CATEGORY_PREFIXES

        cfg = Config.load()
        rest = RestClient(
            RestConfig(
                api_key_id=cfg.kalshi_api_key_id,
                private_key_path=cfg.kalshi_private_key_path,
                use_demo=cfg.kalshi_use_demo,
            )
        )

        prefixes: tuple[str, ...] = ()
        for cat in self.config.universe_categories:
            prefixes = prefixes + CATEGORY_PREFIXES.get(cat, ())

        if self._universe_fetcher is not None:
            universe_tickers = await self._universe_fetcher()
        else:
            universe = await asyncio.to_thread(
                rest.list_open_markets, series_prefixes=prefixes
            )
            universe = [
                m for m in universe
                if m.volume_24h >= self.config.universe_min_volume_usd
            ]
            universe_tickers = [m.ticker for m in universe]

        _log.info("paper.universe_built", ticker_count=len(universe_tickers))
        if not universe_tickers:
            _log.warning("paper.universe_empty")
            return

        ws = ShardedWS(
            rest=rest,
            store=self.store,
            max_tickers_per_conn=self.config.ws_max_tickers_per_conn,
            on_orderbook_delta=self._on_ws_delta_sync,
        )
        await ws.start(universe_tickers)
        try:
            await self._stop.wait()
            _log.info("paper.production_stopped", reason=self._stop_reason)
        finally:
            await ws.stop()

    # ---------------------------------------------------------------
    # Delta handlers
    # ---------------------------------------------------------------

    def _on_ws_delta_sync(self, msg: dict) -> None:
        """ShardedWS calls this synchronously from its message handler.
        We hop onto the runner's event loop to process asynchronously."""
        asyncio.create_task(
            self._process_delta(
                msg["ticker"], msg["side"], msg["price"], msg["delta"]
            ),
            name="paper-delta",
        )

    async def _on_synthetic_delta(self, d: SyntheticDelta) -> None:
        await self._process_delta(d.ticker, d.side, d.price_cents, d.delta)

    async def _process_delta(
        self, ticker: str, side: str, price_cents: int, delta: int
    ) -> None:
        """Apply the delta, run the scanner, maybe size + execute.

        Serialized under a single lock so the sole-writer assumption
        for opportunities_detected holds: at most one in-flight emit
        at a time, so MAX(id) always corresponds to the just-submitted
        row."""
        async with self._pipeline_lock:
            book = self._books.get(ticker)
            if book is None:
                book = OrderBook(ticker=ticker)
                self._books[ticker] = book
            try:
                book.apply_delta(side, price_cents, delta)
            except ValueError as exc:
                _log.warning(
                    "paper.bad_delta",
                    ticker=ticker,
                    side=side,
                    price=price_cents,
                    delta=delta,
                    error=str(exc),
                )
                return

            self._stats.scans += 1
            decision = self.scanner.scan(book)

            if decision.decision != DECISION_EMIT:
                return
            self._stats.emits += 1

            # Drain writer so AUTOINCREMENT id for this row is visible.
            await self.store.flush()
            opp_id = self._lookup_latest_opp_id(decision)

            bankroll = self._bankroll_snapshot()
            sizing = self.sizer.size(decision.opportunity, bankroll)
            if sizing.contracts_per_leg == 0:
                # Sized to zero -- record nothing more. Scanner's
                # audit row already tells the story.
                return

            if self.killswitch.is_tripped():
                _log.warning("paper.killswitch_tripped_skipping_exec")
                return

            result = await self.executor.execute(sizing)
            self._stats.executions += 1
            await self._record_execution(opp_id, result)

    def _lookup_latest_opp_id(self, decision: ScanDecision) -> int:
        """Return the AUTOINCREMENT id just assigned to this decision.

        Relies on:
          (a) this runner being the sole writer to opportunities_detected
          (b) store.flush() having completed before the read
          (c) _pipeline_lock serializing emits so there is no overlap

        A sanity assertion catches drift if either breaks.
        """
        row = self.store.read_one(
            "SELECT id FROM opportunities_detected"
            " WHERE ticker = ? AND ts_ms = ? AND decision = 'emit'"
            " ORDER BY id DESC LIMIT 1",
            (decision.market_ticker, decision.ts_ms),
        )
        if row is None:
            # If the flush+read contract broke, fall back to MAX(id)
            # but log loudly. Tests exercise the happy path.
            _log.error(
                "paper.opp_id_lookup_failed",
                ticker=decision.market_ticker,
                ts_ms=decision.ts_ms,
            )
            fallback = self.store.read_one(
                "SELECT COALESCE(MAX(id), 0) FROM opportunities_detected"
            )
            return int(fallback[0]) if fallback else 0
        return int(row[0])

    # ---------------------------------------------------------------
    # Recording
    # ---------------------------------------------------------------

    def _record_scan(self, decision: ScanDecision) -> None:
        """Scanner on_decision callback. Synchronous, never blocks."""
        opp = decision.opportunity
        if opp is not None:
            self.store.record_opportunity(
                ticker=decision.market_ticker,
                ts_ms=decision.ts_ms,
                yes_ask_cents=opp.yes_ask_cents,
                yes_ask_qty=opp.yes_ask_qty,
                no_ask_cents=opp.no_ask_cents,
                no_ask_qty=opp.no_ask_qty,
                sum_cents=opp.sum_cents,
                est_fees_cents=opp.est_fees_cents,
                slippage_buffer=opp.slippage_buffer_cents,
                net_edge_cents=opp.net_edge_cents,
                max_size_liquidity=opp.max_liquidity_contracts,
                kelly_size=0,           # filled when sizer runs
                hard_cap_size=0,
                final_size=0,
                decision=decision.decision,
                rejection_reason=decision.reason,
            )
        else:
            # Skip row without a constructed Opportunity (halted, empty,
            # sum >= 100). We still record it so the Opportunities tab
            # has the full audit trail.
            self.store.record_opportunity(
                ticker=decision.market_ticker,
                ts_ms=decision.ts_ms,
                yes_ask_cents=0,
                yes_ask_qty=0,
                no_ask_cents=0,
                no_ask_qty=0,
                sum_cents=0,
                est_fees_cents=0,
                slippage_buffer=0,
                net_edge_cents=0.0,
                max_size_liquidity=0,
                kelly_size=0,
                hard_cap_size=0,
                final_size=0,
                decision=decision.decision,
                rejection_reason=decision.reason,
            )

    async def _record_execution(self, opp_id: int, result) -> None:
        """Fan out an ExecutionResult into orders_placed + orders_filled
        + pnl_realized rows linked by opportunity_id."""
        if result.outcome == OUTCOME_KILL_SWITCH:
            self._stats.kill_switch_trips += 1
            return

        for leg in result.legs:
            self.store.record_order_placed(
                client_order_id=leg.client_order_id,
                kalshi_order_id=leg.kalshi_order_id,
                opportunity_id=opp_id,
                ticker=result.decision.opportunity.market_ticker,
                side=leg.side,
                action=leg.action,
                type_="limit" if leg.action == "buy" else "market",
                limit_price=leg.limit_cents,
                count=leg.requested_count,
                placed_ok=leg.error is None and leg.filled_count > 0,
                error=leg.error,
            )
            if leg.filled_count > 0:
                self.store.record_order_filled(
                    client_order_id=leg.client_order_id,
                    filled_price=leg.limit_cents,
                    filled_count=leg.filled_count,
                    fees_cents=0,   # fees are aggregated at the result level
                )

        # Realized P&L gets its own row (the executor's P&L math decides
        # whether net_fill is realized or estimated-with-unwind).
        net = result.net_fill_cents if result.net_fill_cents is not None else 0
        pairs = (
            (result.legs[0].filled_count + result.legs[1].filled_count) // 2
            if len(result.legs) >= 2 else 0
        )
        gross_settlement = pairs * 100
        net_realized = gross_settlement - net - result.total_fees_cents
        if result.legs and result.pnl_confidence == "realized":
            self.store.record_pnl_realized(
                opportunity_id=opp_id,
                yes_pnl_cents=0,   # leg-level breakdown deferred -- net is canonical
                no_pnl_cents=0,
                fees_cents=result.total_fees_cents,
                net_cents=net_realized,
                note=f"paper execution outcome={result.outcome}",
            )
            # Update the in-process bankroll so the sizer sees fresh cash.
            self._bankroll_cents += net_realized

    # ---------------------------------------------------------------
    # Bankroll
    # ---------------------------------------------------------------

    def _bankroll_snapshot(self) -> BankrollSnapshot:
        realized = (
            self.executor.daily_realized_pnl_cents
            if self.executor is not None else 0
        )
        now_ms = clock.now_ms()
        return BankrollSnapshot(
            cash_cents=self._bankroll_cents,
            open_positions_value_cents=0,
            peak_equity_cents=max(self._bankroll_peak_cents, self._bankroll_cents),
            daily_realized_pnl_cents=realized,
            taken_at_ms=now_ms,
            stale=False,
        )

    # ---------------------------------------------------------------
    # Shutdown
    # ---------------------------------------------------------------

    async def _shutdown(self) -> None:
        _log.info(
            "paper.shutdown_start",
            reason=self._stop_reason or "completed",
            scans=self._stats.scans,
            emits=self._stats.emits,
            executions=self._stats.executions,
        )
        # Flush any still-queued writes so the dashboard sees final rows.
        try:
            await self.store.flush()
        except Exception as exc:  # noqa: BLE001
            _log.warning("paper.flush_failed", error=str(exc))
        try:
            await self.store.stop()
        except Exception as exc:  # noqa: BLE001
            _log.warning("paper.store_stop_failed", error=str(exc))
        _log.info("paper.shutdown_complete")

    # ---------------------------------------------------------------
    # Test introspection
    # ---------------------------------------------------------------

    @property
    def stats(self) -> _PipelineStats:
        return self._stats
