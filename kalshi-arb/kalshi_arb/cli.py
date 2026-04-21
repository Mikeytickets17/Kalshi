"""Command-line entry points."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import typer

from . import log
from .config import CATEGORY_PREFIXES, Config

app = typer.Typer(help="Kalshi structural arbitrage engine")


PROD_COUNTDOWN_SECONDS = 5


def _env_bool(key: str) -> bool:
    return os.environ.get(key, "").strip().lower() in ("1", "true", "yes", "on")


@app.command()
def probe(
    env: str = typer.Option(
        "demo",
        "--env",
        help="Probe target: 'demo' (default, safe) or 'prod' (real money endpoints).",
    ),
    timeout: float = typer.Option(
        180.0,
        "--timeout",
        help="Total wall-clock budget (seconds). Suite fails clean on overrun.",
    ),
    e2e_wait: float = typer.Option(
        30.0,
        "--e2e-wait",
        help="Seconds to observe WS events for the end-to-end loop probe.",
    ),
    rest_samples: int = typer.Option(
        100,
        "--rest-samples",
        help="Number of 1c BUY YES orders to fire for the REST write latency probe.",
    ),
) -> None:
    """Run the one-shot probe suite (WS cap + REST latency + rate limit + E2E loop).

    --env demo (default): Safe, informational. Runs against the demo
    environment. Writes config/detected_limits.yaml regardless of
    results -- demo numbers are useful for local development.

    --env prod: Runs against production. Refuses unless
    KALSHI_USE_DEMO=false is explicitly set. Prints a 5-second countdown
    banner, then hits real Kalshi. Every order is a 1c BUY YES limit
    tagged `probe-`, cancelled immediately. ProbeFailure aborts the
    whole run with NO detected_limits.yaml written -- partial results
    would poison the paper CLI's startup gate.
    """
    from .probe.probe import ProbeFailure, run as probe_run

    log.setup()

    env = env.lower().strip()
    if env not in ("demo", "prod"):
        typer.secho(
            f"ERROR: --env must be 'demo' or 'prod' (got {env!r}).",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=2) from None

    if env == "prod":
        # Hard gates BEFORE we do anything network-touching. Accept
        # only an EXPLICIT KALSHI_USE_DEMO=false (or 0/no/off). Unset,
        # any truthy value, or whitespace is a hard refusal.
        raw = os.environ.get("KALSHI_USE_DEMO", "<unset>")
        if raw.strip().lower() not in ("false", "0", "no", "off"):
            typer.secho(
                f"ERROR: --env prod requires KALSHI_USE_DEMO=false "
                f"(got {raw!r}).\n"
                f"Set it explicitly in .env before running the prod probe.",
                fg=typer.colors.RED, err=True,
            )
            raise typer.Exit(code=3) from None
        try:
            cfg = Config.load()
        except RuntimeError as exc:
            typer.secho(f"ERROR: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=2) from None
        if cfg.kalshi_use_demo:
            typer.secho(
                "ERROR: Config still reports demo=True despite "
                "KALSHI_USE_DEMO=false. Restart shell / re-source .env.",
                fg=typer.colors.RED, err=True,
            )
            raise typer.Exit(code=3) from None
        if not cfg.kalshi_api_key_id:
            typer.secho(
                "ERROR: KALSHI_API_KEY_ID not set. Paste your production "
                "key into .env before running --env prod.",
                fg=typer.colors.RED, err=True,
            )
            raise typer.Exit(code=3) from None

        _print_prod_banner(timeout)
        try:
            _countdown(PROD_COUNTDOWN_SECONDS)
        except KeyboardInterrupt:
            typer.secho(
                "\nABORTED by operator during countdown. "
                "No orders placed.", fg=typer.colors.YELLOW, err=True,
            )
            raise typer.Exit(code=0) from None

    try:
        results = asyncio.run(
            probe_run(
                env=env,
                timeout_sec=timeout,
                e2e_wait_sec=e2e_wait,
                rest_write_samples=rest_samples,
            )
        )
    except ProbeFailure as exc:
        typer.secho(
            f"\nPROBE FAILED ({env}): {exc}\n"
            f"detected_limits.yaml was NOT written. Fix the issue and re-run.",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=10) from None
    except RuntimeError as exc:
        typer.secho(f"ERROR: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=4) from None

    typer.secho(
        f"\nPROBE PASSED ({env}). Results written to config/detected_limits.yaml.\n"
        f"  ws.max_confirmed_tickers = {results.ws_subscription.get('max_confirmed_tickers')}\n"
        f"  rest_write.p95_ms        = {results.rest_write_latency_ms.get('p95_ms')}\n"
        f"  rate_limit.ceiling_rps   = {results.rest_rate_limit.get('limit_hit_at_rps') or '> ' + str(results.rest_rate_limit.get('max_successful_rps'))}\n"
        f"  e2e.p95_ms               = {results.end_to_end_loop_ms.get('p95_ms', 'deferred')}",
        fg=typer.colors.GREEN,
    )


def _print_prod_banner(timeout: float) -> None:
    banner = (
        "\n"
        + "=" * 70 + "\n"
        + "  ABOUT TO CONNECT TO PRODUCTION KALSHI.\n"
        + "\n"
        + "  No orders will be placed that can fill. Every order is a 1c\n"
        + "  BUY YES limit, cancelled immediately, tagged with a 'probe-'\n"
        + "  client_order_id. The whole run fails clean on any error or\n"
        + f"  after {int(timeout)} seconds.\n"
        + "\n"
        + f"  Press Ctrl+C within {PROD_COUNTDOWN_SECONDS} seconds to abort.\n"
        + "=" * 70 + "\n"
    )
    sys.stderr.write(banner)
    sys.stderr.flush()


def _countdown(seconds: int) -> None:
    for i in range(seconds, 0, -1):
        sys.stderr.write(f"  starting in {i}...\n")
        sys.stderr.flush()
        time.sleep(1)
    sys.stderr.write("  starting now.\n\n")
    sys.stderr.flush()


@app.command()
def paper(
    smoke_test: int = typer.Option(
        0,
        "--smoke-test",
        help=(
            "Run for N seconds against a fake WS source then exit. "
            "0 = production paper mode (real Kalshi WS)."
        ),
    ),
    smoke_rate: float = typer.Option(
        5.0,
        "--smoke-rate",
        help="Deltas per second in smoke-test mode.",
    ),
    smoke_seed: int = typer.Option(
        42,
        "--smoke-seed",
        help="RNG seed for the fake WS generator in smoke-test mode.",
    ),
    probe_path: Path = typer.Option(
        Path("config/detected_limits.yaml"),
        "--probe-path",
        help="Path to the prod probe output. Must be fresh (<24h) and environment=prod.",
    ),
) -> None:
    """Paper-mode pipeline: Kalshi WS -> scanner -> sizer -> PaperKalshiAPI -> event store.

    The paper command is paper-mode ONLY. It refuses to start if
    LIVE_TRADING=true is set in the environment. Live trading gets a
    separate CLI command (not yet built).
    """
    from .common.gates import GateError
    from .executor.paper import PaperConfig
    from .paper.runner import PaperRunner, PaperRunnerConfig

    log.setup()

    try:
        cfg = Config.load()
    except RuntimeError as exc:
        typer.secho(f"ERROR: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from None

    paper_cfg = PaperConfig.load()
    runner_cfg = PaperRunnerConfig.from_config(
        cfg,
        paper_cfg,
        probe_path=probe_path,
        smoke_test_seconds=smoke_test,
        smoke_test_rate_per_sec=smoke_rate,
        smoke_test_seed=smoke_seed,
    )
    runner = PaperRunner(runner_cfg)

    try:
        asyncio.run(runner.run())
    except GateError as exc:
        typer.secho(f"GATE REFUSED: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=3) from None
    except RuntimeError as exc:
        typer.secho(f"ERROR: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=4) from None
    except KeyboardInterrupt:
        # Signal handler already tripped stop(); _shutdown ran in the
        # finally block of run(). Exit 0 for clean operator Ctrl+C.
        sys.exit(0)


@app.command()
def ingest() -> None:
    """Start the universe + WS consumer + event store in paper mode."""
    from .rest.client import RestClient, RestConfig
    from .store.db import EventStore
    from .ws.consumer import ShardedWS

    log.setup()
    cfg = Config.load()
    rest = RestClient(
        RestConfig(
            api_key_id=cfg.kalshi_api_key_id,
            private_key_path=cfg.kalshi_private_key_path,
            use_demo=cfg.kalshi_use_demo,
        )
    )
    store = EventStore(cfg.event_store_path)

    async def _main() -> None:
        await store.start()
        prefixes: tuple[str, ...] = ()
        for cat in cfg.universe_categories:
            prefixes = prefixes + CATEGORY_PREFIXES.get(cat, ())
        universe = await asyncio.to_thread(
            rest.list_open_markets, series_prefixes=prefixes
        )
        universe.sort(key=lambda m: m.volume_24h, reverse=True)
        universe = [m for m in universe if m.volume_24h >= cfg.universe_min_volume_usd]

        for m in universe:
            store.upsert_market(
                {
                    "ticker": m.ticker,
                    "series_ticker": m.series_ticker,
                    "event_ticker": m.event_ticker,
                    "title": m.title,
                    "subtitle": m.subtitle,
                    "status": m.status,
                    "open_ts_ms": None,
                    "close_ts_ms": m.close_ts_ms,
                    "category": _category_for(m.series_ticker),
                }
            )

        ws = ShardedWS(
            rest=rest,
            store=store,
            max_tickers_per_conn=cfg.ws_max_tickers_per_conn,
        )
        await ws.start([m.ticker for m in universe])
        try:
            while True:
                await asyncio.sleep(60)
        finally:
            await ws.stop()
            await store.stop()

    asyncio.run(_main())


def _category_for(series_ticker: str) -> str:
    for cat, prefixes in CATEGORY_PREFIXES.items():
        if any(series_ticker.startswith(p) for p in prefixes):
            return cat
    return "other"


if __name__ == "__main__":
    app()
