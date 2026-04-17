"""Thin REST facade over pykalshi.

Exposes only what Module 1 actually uses:
- universe discovery (list open markets + last-24h volume)
- per-market order book snapshot (for gap-driven resnapshots)
- health probe endpoints (exchange status, server time)
- order placement / cancellation (used by executor; available but unused in
  Module 1 outside of the probe script's demo writes)

Keeps pykalshi as the single source of auth + signing so we don't duplicate
RSA-PSS code. All network IO is synchronous here — call from within
asyncio.to_thread to avoid blocking the event loop.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pykalshi import KalshiClient as _PyKalshi
from pykalshi._sync.markets import MarketStatus
from pykalshi.aclient import AsyncKalshiClient as _AsyncPyKalshi

from .. import log

_log = log.get("rest.client")


@dataclass(frozen=True)
class RestConfig:
    api_key_id: str
    private_key_path: Path
    use_demo: bool


@dataclass(frozen=True)
class UniverseMarket:
    ticker: str
    series_ticker: str
    event_ticker: str | None
    title: str
    subtitle: str
    status: str
    close_ts_ms: int
    volume_24h: float


class RestClient:
    def __init__(self, cfg: RestConfig) -> None:
        self.cfg = cfg
        self._pyk = _PyKalshi(
            api_key_id=cfg.api_key_id,
            private_key_path=str(cfg.private_key_path),
            demo=cfg.use_demo,
        )
        self._async_pyk: _AsyncPyKalshi | None = None

    @property
    def underlying(self) -> _PyKalshi:
        """Sync pykalshi client for REST calls."""
        return self._pyk

    def async_underlying(self) -> _AsyncPyKalshi:
        """Lazy async pykalshi client. Required for AsyncFeed (the sync Feed
        class is threaded and does NOT support `async with`)."""
        if self._async_pyk is None:
            self._async_pyk = _AsyncPyKalshi(
                api_key_id=self.cfg.api_key_id,
                private_key_path=str(self.cfg.private_key_path),
                demo=self.cfg.use_demo,
            )
        return self._async_pyk

    # ---------- Universe discovery ----------

    def list_open_markets(
        self,
        *,
        series_prefixes: tuple[str, ...] | None = None,
        limit: int | None = None,
    ) -> list[UniverseMarket]:
        """Walk /markets?status=open and filter by series prefix if provided.

        When limit is set we fetch a single capped page instead of paginating
        everything. Probes use this (they only need a ticker pool of a few
        hundred) — avoids a minute-long full scan of demo which has tens of
        thousands of markets across every category.
        """
        out: list[UniverseMarket] = []
        if limit is not None:
            raw = self._pyk.get_markets(
                status=MarketStatus.OPEN, limit=min(limit, 1000), fetch_all=False
            )
        else:
            raw = self._pyk.get_markets(status=MarketStatus.OPEN, fetch_all=True)
        for m in raw or []:
            ticker = str(getattr(m, "ticker", "") or "")
            if not ticker:
                continue
            series = str(getattr(m, "series_ticker", "") or "")
            if series_prefixes and not any(
                series.startswith(p) or ticker.startswith(p) for p in series_prefixes
            ):
                continue
            close_iso = str(getattr(m, "close_time", "") or "")
            close_ts_ms = 0
            if close_iso:
                try:
                    from datetime import datetime

                    close_ts_ms = int(
                        datetime.fromisoformat(close_iso.replace("Z", "+00:00")).timestamp() * 1000
                    )
                except (ValueError, TypeError):
                    close_ts_ms = 0
            vol = float(getattr(m, "volume_24h", 0) or getattr(m, "volume_fp", 0) or 0)
            out.append(
                UniverseMarket(
                    ticker=ticker,
                    series_ticker=series,
                    event_ticker=getattr(m, "event_ticker", None),
                    title=str(getattr(m, "title", "") or ""),
                    subtitle=str(getattr(m, "subtitle", "") or ""),
                    status=str(_enum_value(getattr(m, "status", ""))).lower(),
                    close_ts_ms=close_ts_ms,
                    volume_24h=vol,
                )
            )
        _log.info("rest.universe_fetched", count=len(out), prefixes=series_prefixes)
        return out

    # ---------- Orderbook snapshot (gap recovery) ----------

    def get_orderbook(self, ticker: str, depth: int = 10) -> dict[str, Any]:
        """GET /markets/{ticker}/orderbook — returns {yes: [[price, size]], no: [...]}."""
        # pykalshi exposes this as a method on the client; if signature drifts,
        # adapt here rather than in callers.
        try:
            raw = self._pyk.get_market_orderbook(ticker=ticker, depth=depth)
        except AttributeError:
            # older pykalshi: fallback via raw GET
            raw = self._pyk.get("markets/" + ticker + "/orderbook", params={"depth": depth})
        if hasattr(raw, "model_dump"):
            return raw.model_dump()
        if isinstance(raw, dict):
            return raw
        # Last resort: attribute-scrape
        return {
            "yes": getattr(raw, "yes", []) or [],
            "no": getattr(raw, "no", []) or [],
        }

    # ---------- Health / probe helpers ----------

    def server_time(self) -> int | None:
        try:
            t = self._pyk.get_exchange_status()
            ts = getattr(t, "server_time", None) or getattr(t, "timestamp", None)
            if ts is None:
                return None
            if isinstance(ts, (int, float)):
                return int(ts * 1000) if ts < 1e12 else int(ts)
            return None
        except Exception as exc:  # noqa: BLE001
            _log.warning("rest.server_time_failed", error=str(exc))
            return None

    def ping_ms(self) -> float:
        """Round-trip a cheap GET (exchange status). Returns ms, or -1 on error."""
        t0 = time.monotonic()
        try:
            self._pyk.get_exchange_status()
            return (time.monotonic() - t0) * 1000
        except Exception as exc:  # noqa: BLE001
            _log.warning("rest.ping_failed", error=str(exc))
            return -1.0


def _enum_value(v: Any) -> Any:
    """Normalize pykalshi enum to its wire string."""
    return getattr(v, "value", None) or getattr(v, "name", None) or v
