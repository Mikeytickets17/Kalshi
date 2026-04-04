"""
Wallet ranker module.

Periodically re-scores and updates the wallet watchlist by pulling
performance data from Polymarket leaderboard, Dune Analytics, and other sources.
"""

import json
import logging
import math
import time
from dataclasses import dataclass
from typing import Optional

import httpx

import config

logger = logging.getLogger(__name__)


@dataclass
class WalletStats:
    """Aggregated stats for a wallet from multiple sources."""
    address: str
    alias: str
    win_rate: float
    total_trades: int
    pnl_usdc: float
    recent_win_rate_30d: float
    recent_trades_30d: int
    sharpe_estimate: float
    score: float = 0.0


class WalletRanker:
    """Ranks wallets by performance and updates the watchlist."""

    def __init__(self) -> None:
        self._http = httpx.Client(timeout=30.0)
        self._dune_api_key: str = config.DUNE_API_KEY
        self._watchlist_file: str = config.WATCHLIST_FILE
        self._min_score: float = config.MIN_WALLET_SCORE
        self._last_run: float = 0.0

    def should_run(self) -> bool:
        """Check if enough time has passed since last ranking run."""
        interval = config.WALLET_REFRESH_INTERVAL_HOURS * 3600
        return (time.time() - self._last_run) >= interval

    def run(self) -> list[WalletStats]:
        """Execute a full ranking cycle."""
        logger.info("Starting wallet ranking cycle...")
        self._last_run = time.time()

        # Gather stats from all sources
        all_stats: dict[str, WalletStats] = {}

        # Source 1: Polymarket leaderboard
        leaderboard_wallets = self._fetch_polymarket_leaderboard()
        for ws in leaderboard_wallets:
            all_stats[ws.address.lower()] = ws

        # Source 2: Dune Analytics
        dune_wallets = self._fetch_dune_analytics()
        for ws in dune_wallets:
            addr = ws.address.lower()
            if addr in all_stats:
                self._merge_stats(all_stats[addr], ws)
            else:
                all_stats[addr] = ws

        # Source 3: Existing wallets.json data
        existing = self._load_existing_wallets()
        for addr, data in existing.items():
            if addr in all_stats:
                # Preserve alias and manual settings
                all_stats[addr].alias = data.get("alias", all_stats[addr].alias)
            else:
                all_stats[addr] = WalletStats(
                    address=addr,
                    alias=data.get("alias", addr[:8]),
                    win_rate=float(data.get("win_rate", 0)),
                    total_trades=int(data.get("total_trades", 0)),
                    pnl_usdc=float(data.get("verified_pnl_usdc", 0)),
                    recent_win_rate_30d=float(data.get("win_rate", 0)),
                    recent_trades_30d=0,
                    sharpe_estimate=0.0,
                )

        # Score all wallets
        for ws in all_stats.values():
            ws.score = self._compute_score(ws)

        # Sort by score descending
        ranked = sorted(all_stats.values(), key=lambda w: w.score, reverse=True)

        # Save updated watchlist
        self._save_watchlist(ranked)

        logger.info(
            "Wallet ranking complete: %d wallets scored, %d active (score >= %.2f)",
            len(ranked),
            sum(1 for w in ranked if w.score >= self._min_score),
            self._min_score,
        )
        return ranked

    def _compute_score(self, ws: WalletStats) -> float:
        """
        Compute wallet score using the formula:
          score = (win_rate * 0.40) +
                  (log(total_trades) / log(1000) * 0.25) +
                  (sharpe_estimate * 0.20) +
                  (recency_factor * 0.15)
        """
        # Win rate component (0.40 weight)
        win_rate_component = ws.win_rate * 0.40

        # Trade volume component (0.25 weight)
        trade_count = max(ws.total_trades, 1)
        trade_component = min(math.log(trade_count) / math.log(1000), 1.0) * 0.25

        # Sharpe estimate component (0.20 weight)
        if ws.total_trades > 0:
            sharpe = ws.pnl_usdc / math.sqrt(ws.total_trades)
            # Normalize: assume sharpe > 50 is excellent
            sharpe_normalized = min(max(sharpe / 50.0, 0.0), 1.0)
        else:
            sharpe_normalized = 0.0
        ws.sharpe_estimate = sharpe_normalized
        sharpe_component = sharpe_normalized * 0.20

        # Recency factor (0.15 weight): weight recent 30 days more
        if ws.recent_win_rate_30d > 0:
            recency = ws.recent_win_rate_30d
        else:
            recency = ws.win_rate * 0.8  # Discount if no recent data
        recency_component = recency * 0.15

        total = win_rate_component + trade_component + sharpe_component + recency_component
        return round(min(total, 1.0), 4)

    def _fetch_polymarket_leaderboard(self) -> list[WalletStats]:
        """Fetch wallet performance data from Polymarket's public leaderboard."""
        wallets: list[WalletStats] = []
        try:
            resp = self._http.get(
                f"{config.POLYMARKET_GAMMA_URL}/leaderboard",
                params={"limit": 100, "period": "all"},
            )
            resp.raise_for_status()
            data = resp.json()

            for entry in data if isinstance(data, list) else data.get("results", []):
                wallets.append(WalletStats(
                    address=str(entry.get("address", "")),
                    alias=str(entry.get("username", "")),
                    win_rate=float(entry.get("win_rate", 0)),
                    total_trades=int(entry.get("total_trades", 0)),
                    pnl_usdc=float(entry.get("pnl", 0)),
                    recent_win_rate_30d=float(entry.get("recent_win_rate", 0)),
                    recent_trades_30d=int(entry.get("recent_trades", 0)),
                    sharpe_estimate=0.0,
                ))
        except Exception as exc:
            logger.warning("Failed to fetch Polymarket leaderboard: %s", exc)
        return wallets

    def _fetch_dune_analytics(self) -> list[WalletStats]:
        """Fetch wallet data from Dune Analytics query."""
        if not self._dune_api_key:
            logger.debug("No Dune API key configured, skipping Dune source")
            return []

        wallets: list[WalletStats] = []
        try:
            # Default Dune query ID for Polymarket trader performance
            query_id = "3500000"  # Replace with actual query ID
            resp = self._http.get(
                f"https://api.dune.com/api/v1/query/{query_id}/results",
                headers={"X-Dune-API-Key": self._dune_api_key},
            )
            resp.raise_for_status()
            rows = resp.json().get("result", {}).get("rows", [])

            for row in rows:
                wallets.append(WalletStats(
                    address=str(row.get("trader_address", "")),
                    alias="",
                    win_rate=float(row.get("win_rate", 0)),
                    total_trades=int(row.get("trade_count", 0)),
                    pnl_usdc=float(row.get("total_pnl_usdc", 0)),
                    recent_win_rate_30d=float(row.get("win_rate_30d", 0)),
                    recent_trades_30d=int(row.get("trades_30d", 0)),
                    sharpe_estimate=0.0,
                ))
        except Exception as exc:
            logger.warning("Failed to fetch Dune Analytics data: %s", exc)
        return wallets

    def _merge_stats(self, existing: WalletStats, new: WalletStats) -> None:
        """Merge new stats into existing, preferring non-zero values."""
        if new.win_rate > 0:
            existing.win_rate = (existing.win_rate + new.win_rate) / 2
        if new.total_trades > existing.total_trades:
            existing.total_trades = new.total_trades
        if abs(new.pnl_usdc) > abs(existing.pnl_usdc):
            existing.pnl_usdc = new.pnl_usdc
        if new.recent_win_rate_30d > 0:
            existing.recent_win_rate_30d = new.recent_win_rate_30d

    def _load_existing_wallets(self) -> dict[str, dict]:
        """Load existing wallets from wallets.json."""
        try:
            with open(self._watchlist_file, "r") as f:
                data = json.load(f)
            wallets_list = data if isinstance(data, list) else data.get("wallets", [])
            return {w["address"].lower(): w for w in wallets_list}
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_watchlist(self, ranked: list[WalletStats]) -> None:
        """Save the ranked watchlist to wallets.json."""
        wallets = []
        for ws in ranked:
            wallets.append({
                "address": ws.address,
                "alias": ws.alias or ws.address[:8],
                "win_rate": round(ws.win_rate, 4),
                "total_trades": ws.total_trades,
                "verified_pnl_usdc": round(ws.pnl_usdc, 2),
                "weight": round(min(ws.score, 1.0), 4),
                "active": ws.score >= self._min_score,
                "score": round(ws.score, 4),
            })

        with open(self._watchlist_file, "w") as f:
            json.dump({"wallets": wallets, "last_updated": time.time()}, f, indent=2)

        logger.info("Saved %d wallets to %s", len(wallets), self._watchlist_file)

    def close(self) -> None:
        """Close the HTTP client."""
        self._http.close()
