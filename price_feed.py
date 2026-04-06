"""
CEX price feed module.

Streams real-time BTC/ETH prices from Binance and Coinbase via WebSocket.
Maintains a rolling window of recent prices for edge confirmation.

This is the "source of truth" — when this price diverges from Polymarket's
implied price, that's the arbitrage opportunity.
"""

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import config

logger = logging.getLogger(__name__)


@dataclass
class PriceTick:
    """A single price observation from a CEX."""
    source: str       # "binance" or "coinbase"
    asset: str        # "BTC" or "ETH"
    price: float
    timestamp: float  # unix time with ms precision
    volume_24h: float = 0.0


@dataclass
class PriceState:
    """Aggregated current price state for an asset."""
    asset: str
    binance_price: float = 0.0
    coinbase_price: float = 0.0
    binance_ts: float = 0.0
    coinbase_ts: float = 0.0
    consensus_price: float = 0.0  # weighted average
    last_updated: float = 0.0
    confidence: float = 0.0       # 0-1, based on how fresh and agreeing the feeds are

    def update(self, tick: PriceTick) -> None:
        """Update state with a new price tick."""
        if tick.source == "binance":
            self.binance_price = tick.price
            self.binance_ts = tick.timestamp
        elif tick.source == "coinbase":
            self.coinbase_price = tick.price
            self.coinbase_ts = tick.timestamp

        # Consensus: average of available feeds
        prices = []
        if self.binance_price > 0:
            prices.append(self.binance_price)
        if self.coinbase_price > 0:
            prices.append(self.coinbase_price)

        if prices:
            self.consensus_price = sum(prices) / len(prices)

        self.last_updated = time.time()

        # Confidence: high if both feeds agree and are fresh
        if len(prices) == 2:
            spread = abs(self.binance_price - self.coinbase_price) / self.consensus_price
            freshness = min(1.0, 1.0 - (time.time() - min(self.binance_ts, self.coinbase_ts)) / 5.0)
            self.confidence = max(0, (1.0 - spread * 50) * freshness)
        elif len(prices) == 1:
            self.confidence = 0.7  # Single source, lower confidence
        else:
            self.confidence = 0.0


class PriceFeed:
    """Streams real-time prices from Binance and Coinbase."""

    def __init__(self) -> None:
        self._running = False
        self._prices: dict[str, PriceState] = {}
        for asset in config.TARGET_ASSETS:
            self._prices[asset] = PriceState(asset=asset)
        # Rolling history for trend detection
        self._history: dict[str, deque[PriceTick]] = {
            asset: deque(maxlen=500) for asset in config.TARGET_ASSETS
        }

    def get_price(self, asset: str) -> Optional[PriceState]:
        """Get current aggregated price for an asset."""
        state = self._prices.get(asset)
        if state and state.confidence > 0 and state.consensus_price > 0:
            return state
        return None

    async def start(self) -> None:
        """Start all price feed connections."""
        self._running = True
        logger.info("PriceFeed starting for assets: %s", config.TARGET_ASSETS)

        if config.PAPER_MODE:
            await self._run_paper_mode()
        else:
            tasks = [
                asyncio.create_task(self._run_binance(), name="binance_feed"),
                asyncio.create_task(self._run_coinbase(), name="coinbase_feed"),
            ]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)

    async def stop(self) -> None:
        self._running = False
        logger.info("PriceFeed stopped")

    # --- Binance WebSocket ---

    async def _run_binance(self) -> None:
        """Connect to Binance WebSocket for real-time trades."""
        import websockets

        # Subscribe to trade streams for all target assets
        streams = []
        for asset in config.TARGET_ASSETS:
            symbol = f"{asset.lower()}usdt"
            streams.append(f"{symbol}@trade")
        url = f"{config.BINANCE_WS_URL}/{'/'.join(streams)}"

        while self._running:
            try:
                logger.info("Connecting to Binance WebSocket...")
                async with websockets.connect(url, ping_interval=20) as ws:
                    logger.info("Binance WebSocket connected")
                    while self._running:
                        msg = await asyncio.wait_for(ws.recv(), timeout=30)
                        data = json.loads(msg)
                        self._handle_binance_trade(data)
            except asyncio.TimeoutError:
                logger.warning("Binance WebSocket timeout, reconnecting...")
            except Exception as exc:
                logger.error("Binance WebSocket error: %s, reconnecting in 2s...", exc)
                await asyncio.sleep(2)

    def _handle_binance_trade(self, data: dict) -> None:
        """Process a Binance trade message."""
        symbol = data.get("s", "").upper()
        price = float(data.get("p", 0))
        ts = float(data.get("T", 0)) / 1000.0  # Binance gives ms

        asset = ""
        if symbol.startswith("BTC"):
            asset = "BTC"
        elif symbol.startswith("ETH"):
            asset = "ETH"
        else:
            return

        if asset not in self._prices:
            return

        tick = PriceTick(source="binance", asset=asset, price=price, timestamp=ts)
        self._prices[asset].update(tick)
        self._history[asset].append(tick)

    # --- Coinbase WebSocket ---

    async def _run_coinbase(self) -> None:
        """Connect to Coinbase WebSocket for real-time trades."""
        import websockets

        product_ids = []
        for asset in config.TARGET_ASSETS:
            product_ids.append(f"{asset}-USD")

        while self._running:
            try:
                logger.info("Connecting to Coinbase WebSocket...")
                async with websockets.connect(config.COINBASE_WS_URL, ping_interval=20) as ws:
                    sub = json.dumps({
                        "type": "subscribe",
                        "product_ids": product_ids,
                        "channels": ["ticker"],
                    })
                    await ws.send(sub)
                    logger.info("Coinbase WebSocket connected")
                    while self._running:
                        msg = await asyncio.wait_for(ws.recv(), timeout=30)
                        data = json.loads(msg)
                        if data.get("type") == "ticker":
                            self._handle_coinbase_ticker(data)
            except asyncio.TimeoutError:
                logger.warning("Coinbase WebSocket timeout, reconnecting...")
            except Exception as exc:
                logger.error("Coinbase WebSocket error: %s, reconnecting in 2s...", exc)
                await asyncio.sleep(2)

    def _handle_coinbase_ticker(self, data: dict) -> None:
        """Process a Coinbase ticker message."""
        product = data.get("product_id", "")
        price = float(data.get("price", 0))

        asset = product.split("-")[0].upper()
        if asset not in self._prices:
            return

        tick = PriceTick(
            source="coinbase", asset=asset, price=price,
            timestamp=time.time(),
            volume_24h=float(data.get("volume_24h", 0)),
        )
        self._prices[asset].update(tick)
        self._history[asset].append(tick)

    # --- Paper Mode ---

    async def _run_paper_mode(self) -> None:
        """Simulate price feed with realistic BTC/ETH price movements."""
        logger.info("[PAPER] PriceFeed running in simulation mode")

        import random
        # Start with realistic prices
        sim_prices = {"BTC": 68500.0, "ETH": 3450.0}
        volatility = {"BTC": 0.0003, "ETH": 0.0004}  # per-tick vol

        while self._running:
            for asset in config.TARGET_ASSETS:
                if asset not in sim_prices:
                    continue

                # Random walk with slight mean reversion
                base = sim_prices[asset]
                move = base * random.gauss(0, volatility[asset])
                sim_prices[asset] = base + move

                # Binance tick (slightly ahead)
                b_price = sim_prices[asset] + random.gauss(0, base * 0.00005)
                b_tick = PriceTick("binance", asset, round(b_price, 2), time.time())
                self._prices[asset].update(b_tick)
                self._history[asset].append(b_tick)

                # Coinbase tick (slightly behind, adds noise)
                c_delay = random.uniform(0.05, 0.3)
                c_price = sim_prices[asset] + random.gauss(0, base * 0.00008)
                c_tick = PriceTick("coinbase", asset, round(c_price, 2), time.time() - c_delay)
                self._prices[asset].update(c_tick)
                self._history[asset].append(c_tick)

            # Simulate ~10 ticks per second
            await asyncio.sleep(0.1)
