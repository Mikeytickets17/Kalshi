"""Command-line entry points."""

from __future__ import annotations

import asyncio

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
