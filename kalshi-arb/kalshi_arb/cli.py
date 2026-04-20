"""Command-line entry points."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import typer

from . import log
from .config import CATEGORY_PREFIXES, Config

app = typer.Typer(help="Kalshi structural arbitrage engine")


@app.command()
def probe() -> None:
    """Run the one-shot probe (WS cap + REST latency + rate limit + E2E loop)."""
    from .probe.probe import run as probe_run

    log.setup()
    asyncio.run(probe_run())


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
