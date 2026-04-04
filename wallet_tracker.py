"""
Wallet tracker module.

Monitors watched wallets for new Polymarket transactions on Polygon
via WebSocket (Alchemy/QuickNode) or polling fallback.
Emits TradeSignal objects for the signal evaluator.
"""

import asyncio
import json
import logging
import os
import random
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

import config

logger = logging.getLogger(__name__)


@dataclass
class WalletEntry:
    """A watched wallet from wallets.json."""
    address: str
    alias: str = ""
    win_rate: float = 0.0
    total_trades: int = 0
    verified_pnl_usdc: float = 0.0
    weight: float = 0.5
    active: bool = True


@dataclass
class TradeSignal:
    """Decoded trade detected from a watched wallet."""
    wallet_address: str
    wallet_alias: str
    wallet_weight: float
    market_id: str
    condition_id: str
    side: str  # "YES" or "NO"
    size_usdc: float
    price: float
    tx_hash: str
    block_number: int
    timestamp: float = field(default_factory=time.time)
    wallet_win_rate: float = 0.0
    wallet_portfolio_pct: float = 0.0  # what % of wallet's portfolio this trade represents


# Known Polymarket contract addresses on Polygon
POLYMARKET_CONTRACTS: set[str] = {
    config.POLYMARKET_CTF_EXCHANGE.lower(),
    config.POLYMARKET_NEG_RISK_EXCHANGE.lower(),
}

# Minimal ABI fragments for decoding Polymarket trade calls
TRADE_FUNCTION_SIGS: dict[str, str] = {
    "0x": "fillOrder",
    "0xa6dfbc7f": "fillOrder",
    "0x0c51b88f": "fillOrders",
}


class WalletTracker:
    """Tracks watched wallets for Polymarket trades on Polygon."""

    def __init__(self) -> None:
        self._wallets: dict[str, WalletEntry] = {}
        self._signal_queue: asyncio.Queue[TradeSignal] = asyncio.Queue()
        self._running: bool = False
        self._ws_url: str = config.POLYGON_RPC_WS
        self._paper_mode: bool = config.PAPER_MODE
        self._watchlist_file: str = config.WATCHLIST_FILE
        self._last_watchlist_mtime: float = 0.0
        self._http = httpx.AsyncClient(timeout=30.0)
        self.load_watchlist()

    @property
    def signal_queue(self) -> asyncio.Queue[TradeSignal]:
        """Queue of detected trade signals."""
        return self._signal_queue

    @property
    def watched_wallets(self) -> dict[str, WalletEntry]:
        """Currently watched wallets."""
        return self._wallets

    def load_watchlist(self) -> None:
        """Load or reload the wallet watchlist from wallets.json."""
        try:
            with open(self._watchlist_file, "r") as f:
                data = json.load(f)
            wallets_list = data if isinstance(data, list) else data.get("wallets", [])
            new_wallets: dict[str, WalletEntry] = {}
            for w in wallets_list:
                addr = w["address"].lower()
                new_wallets[addr] = WalletEntry(
                    address=addr,
                    alias=w.get("alias", addr[:8]),
                    win_rate=float(w.get("win_rate", 0.0)),
                    total_trades=int(w.get("total_trades", 0)),
                    verified_pnl_usdc=float(w.get("verified_pnl_usdc", 0.0)),
                    weight=float(w.get("weight", 0.5)),
                    active=bool(w.get("active", True)),
                )
            active_count = sum(1 for w in new_wallets.values() if w.active)
            logger.info(
                "Loaded %d wallets (%d active) from %s",
                len(new_wallets), active_count, self._watchlist_file,
            )
            self._wallets = new_wallets
            self._last_watchlist_mtime = os.path.getmtime(self._watchlist_file)
        except FileNotFoundError:
            logger.warning("Watchlist file %s not found, starting with empty list", self._watchlist_file)
        except Exception as exc:
            logger.error("Failed to load watchlist: %s", exc)

    def _check_hot_reload(self) -> None:
        """Reload watchlist if the file has been modified."""
        try:
            current_mtime = os.path.getmtime(self._watchlist_file)
            if current_mtime > self._last_watchlist_mtime:
                logger.info("Watchlist file changed, hot-reloading...")
                self.load_watchlist()
        except FileNotFoundError:
            pass

    def get_active_addresses(self) -> list[str]:
        """Return list of active wallet addresses."""
        return [addr for addr, w in self._wallets.items() if w.active]

    async def start(self) -> None:
        """Start the wallet tracker."""
        self._running = True
        logger.info("WalletTracker starting (paper_mode=%s)", self._paper_mode)

        if self._paper_mode:
            await self._run_paper_mode()
        elif self._ws_url:
            await self._run_websocket()
        else:
            await self._run_polling()

    async def stop(self) -> None:
        """Stop the wallet tracker."""
        self._running = False
        await self._http.aclose()
        logger.info("WalletTracker stopped")

    # --- Paper Mode: simulate trade detections ---

    async def _run_paper_mode(self) -> None:
        """In paper mode, periodically emit simulated trade signals."""
        logger.info("[PAPER] WalletTracker running in simulation mode")
        sample_markets = [
            {"market_id": "pm-will-btc-hit-100k", "condition_id": "0xabc123", "question": "Will BTC hit $100k?"},
            {"market_id": "pm-us-election-2024", "condition_id": "0xdef456", "question": "US Election 2024"},
            {"market_id": "pm-fed-rate-cut", "condition_id": "0xghi789", "question": "Fed rate cut by June?"},
        ]

        while self._running:
            self._check_hot_reload()
            active_wallets = [w for w in self._wallets.values() if w.active]
            if not active_wallets:
                await asyncio.sleep(10)
                continue

            # Simulate a trade detection every 30-120 seconds in paper mode
            delay = random.uniform(30, 120)
            await asyncio.sleep(delay)
            if not self._running:
                break

            # Add simulated detection delay
            detection_delay = random.uniform(
                config.PAPER_DETECTION_DELAY_MIN,
                config.PAPER_DETECTION_DELAY_MAX,
            )
            await asyncio.sleep(detection_delay)

            wallet = random.choice(active_wallets)
            market = random.choice(sample_markets)
            side = random.choice(["YES", "NO"])
            size = round(random.uniform(50, 500), 2)
            price = round(random.uniform(0.20, 0.80), 4)

            signal = TradeSignal(
                wallet_address=wallet.address,
                wallet_alias=wallet.alias,
                wallet_weight=wallet.weight,
                market_id=market["market_id"],
                condition_id=market["condition_id"],
                side=side,
                size_usdc=size,
                price=price,
                tx_hash=f"0xpaper{int(time.time())}",
                block_number=0,
                wallet_win_rate=wallet.win_rate,
                wallet_portfolio_pct=round(random.uniform(0.01, 0.10), 4),
            )

            logger.info(
                "[PAPER] Detected trade: wallet=%s market=%s side=%s size=%.2f price=%.4f",
                wallet.alias, market["market_id"], side, size, price,
            )
            await self._signal_queue.put(signal)

    # --- WebSocket Mode ---

    async def _run_websocket(self) -> None:
        """Connect to Polygon WebSocket and subscribe to wallet transactions."""
        try:
            import websockets
        except ImportError:
            logger.error("websockets package not installed, falling back to polling")
            await self._run_polling()
            return

        while self._running:
            try:
                logger.info("Connecting to Polygon WebSocket: %s", self._ws_url[:50] + "...")
                async with websockets.connect(self._ws_url) as ws:
                    # Subscribe to pending transactions for watched addresses
                    active_addrs = self.get_active_addresses()
                    sub_msg = json.dumps({
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "eth_subscribe",
                        "params": ["alchemy_minedTransactions", {
                            "addresses": [
                                {"from": addr} for addr in active_addrs
                            ],
                        }],
                    })
                    await ws.send(sub_msg)
                    resp = await ws.recv()
                    logger.info("WebSocket subscription response: %s", resp)

                    while self._running:
                        self._check_hot_reload()
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=60)
                            await self._handle_ws_message(json.loads(msg))
                        except asyncio.TimeoutError:
                            # Send ping to keep connection alive
                            await ws.ping()
            except Exception as exc:
                logger.error("WebSocket error: %s, reconnecting in 5s...", exc)
                await asyncio.sleep(5)

    async def _handle_ws_message(self, msg: dict) -> None:
        """Process a WebSocket message containing a transaction."""
        params = msg.get("params", {})
        result = params.get("result", {})
        tx = result.get("transaction", {})
        if not tx:
            return

        to_addr = (tx.get("to") or "").lower()
        from_addr = (tx.get("from") or "").lower()

        # Only process transactions to Polymarket contracts
        if to_addr not in POLYMARKET_CONTRACTS:
            return

        # Only process from watched wallets
        if from_addr not in self._wallets:
            return

        wallet = self._wallets[from_addr]
        if not wallet.active:
            return

        signal = self._decode_transaction(tx, wallet)
        if signal:
            logger.info(
                "Detected trade: wallet=%s market=%s side=%s size=%.2f",
                wallet.alias, signal.market_id, signal.side, signal.size_usdc,
            )
            await self._signal_queue.put(signal)

    def _decode_transaction(self, tx: dict, wallet: WalletEntry) -> Optional[TradeSignal]:
        """Decode a Polymarket transaction into a TradeSignal."""
        try:
            input_data = tx.get("input", "")
            if len(input_data) < 10:
                return None

            # Extract function selector
            func_sig = input_data[:10]
            tx_hash = tx.get("hash", "")
            block_number = int(tx.get("blockNumber", "0"), 16) if tx.get("blockNumber") else 0

            # Simplified decoding — in production, use full ABI decoding
            # For now, extract what we can from the tx data
            value_wei = int(tx.get("value", "0"), 16) if tx.get("value") else 0

            return TradeSignal(
                wallet_address=wallet.address,
                wallet_alias=wallet.alias,
                wallet_weight=wallet.weight,
                market_id=f"decoded-{tx_hash[:10]}",
                condition_id="",
                side="YES",
                size_usdc=value_wei / 1e6 if value_wei > 0 else 100.0,
                price=0.50,
                tx_hash=tx_hash,
                block_number=block_number,
                wallet_win_rate=wallet.win_rate,
                wallet_portfolio_pct=0.05,
            )
        except Exception as exc:
            logger.error("Failed to decode transaction: %s", exc)
            return None

    # --- Polling Fallback ---

    async def _run_polling(self) -> None:
        """Poll for new transactions via HTTP RPC (fallback when no WebSocket)."""
        logger.info("WalletTracker running in polling mode")
        rpc_url = config.POLYGON_RPC_WS.replace("wss://", "https://").replace("ws://", "http://")
        if not rpc_url:
            logger.warning("No RPC URL configured, wallet tracking disabled")
            while self._running:
                await asyncio.sleep(10)
            return

        last_block = 0
        while self._running:
            try:
                self._check_hot_reload()
                # Get latest block number
                resp = await self._http.post(rpc_url, json={
                    "jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": [],
                })
                current_block = int(resp.json()["result"], 16)

                if last_block == 0:
                    last_block = current_block - 5

                for addr in self.get_active_addresses():
                    # Get transactions in recent blocks
                    resp = await self._http.post(rpc_url, json={
                        "jsonrpc": "2.0", "id": 1,
                        "method": "eth_getLogs",
                        "params": [{
                            "fromBlock": hex(last_block),
                            "toBlock": hex(current_block),
                            "address": list(POLYMARKET_CONTRACTS),
                            "topics": [None, f"0x000000000000000000000000{addr[2:]}"],
                        }],
                    })
                    logs = resp.json().get("result", [])
                    for log_entry in logs:
                        logger.debug("Found log from %s in block %s", addr, log_entry.get("blockNumber"))

                last_block = current_block
            except Exception as exc:
                logger.error("Polling error: %s", exc)

            await asyncio.sleep(config.POSITION_CHECK_INTERVAL_SECONDS)
