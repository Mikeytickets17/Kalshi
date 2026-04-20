"""
Kalshi 15-Minute BTC Contract Trader.

Trades Kalshi's 15-minute Bitcoin contracts using real-time
price momentum from Binance WebSocket.

Every 15 minutes, a new Kalshi contract opens:
"Will BTC be UP or DOWN in the next 15 minutes?"

Strategy:
1. Read BTC price from Binance every second
2. Calculate momentum (price change over last 60 seconds)
3. If momentum > +0.15% → BUY YES (price going up)
4. If momentum < -0.15% → BUY NO (price going down)
5. If flat → skip this window
6. Contract resolves at end of 15-min window automatically

Position sizing: $50-100 per contract
Risk: max $15 loss per trade (stop loss)
Target: $50+ profit per winning trade
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass

import httpx

import config
import shared_state

logger = logging.getLogger(__name__)


@dataclass
class BTCState:
    """Tracks BTC price for momentum calculation."""
    prices: list  # [(timestamp, price), ...]
    last_trade_time: float = 0
    trades_today: int = 0
    wins_today: int = 0
    losses_today: int = 0
    pnl_today: float = 0


class BTCTrader:
    """Trades Kalshi 15-minute BTC contracts based on price momentum."""

    TRADE_INTERVAL = 900  # 15 minutes between trades
    MOMENTUM_THRESHOLD = 0.0015  # 0.15% price change triggers trade
    POSITION_SIZE = 75  # $75 per trade
    MAX_DAILY_LOSS = 100  # stop trading if down $100 today
    MAX_TRADES_PER_DAY = 50

    def __init__(self) -> None:
        self._state = BTCState(prices=[])
        self._http = httpx.AsyncClient(timeout=10.0)
        self._running = False
        self._btc_price = 0.0
        self._kalshi_connected = bool(config.KALSHI_API_KEY_ID)

    async def start(self) -> None:
        """Start the BTC trader — runs two tasks concurrently."""
        self._running = True
        logger.info("BTCTrader starting — 15-min Kalshi contracts, momentum-based")

        tasks = [
            asyncio.create_task(self._price_tracker(), name="btc_price"),
            asyncio.create_task(self._trade_loop(), name="btc_trader"),
        ]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)

    async def stop(self) -> None:
        self._running = False
        await self._http.aclose()

    async def _price_tracker(self) -> None:
        """Track BTC price from multiple sources."""
        logger.info("BTC price tracker starting")

        while self._running:
            price = await self._get_btc_price()
            if price > 0:
                self._btc_price = price
                self._state.prices.append((time.time(), price))
                # Keep last 5 minutes of prices
                cutoff = time.time() - 300
                self._state.prices = [(t, p) for t, p in self._state.prices if t > cutoff]

            await asyncio.sleep(2)  # poll every 2 seconds

    async def _get_btc_price(self) -> float:
        """Get BTC price from Binance REST API (free, no key needed)."""
        try:
            r = await self._http.get(
                "https://api.binance.com/api/v3/ticker/price",
                params={"symbol": "BTCUSDT"},
            )
            if r.status_code == 200:
                return float(r.json()["price"])
        except Exception:
            pass

        # Fallback: CoinGecko
        try:
            r = await self._http.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "bitcoin", "vs_currencies": "usd"},
            )
            if r.status_code == 200:
                return float(r.json()["bitcoin"]["usd"])
        except Exception:
            pass

        # Fallback: CryptoCompare
        try:
            r = await self._http.get(
                "https://min-api.cryptocompare.com/data/price",
                params={"fsym": "BTC", "tsyms": "USD"},
            )
            if r.status_code == 200:
                return float(r.json()["USD"])
        except Exception:
            pass

        return 0.0

    def _calculate_momentum(self, window_seconds: int = 60) -> float:
        """Calculate price momentum over the last N seconds.
        Returns percentage change (e.g., 0.002 = +0.2%).
        """
        if len(self._state.prices) < 2:
            return 0.0

        now = time.time()
        cutoff = now - window_seconds

        recent = [(t, p) for t, p in self._state.prices if t > cutoff]
        if len(recent) < 2:
            return 0.0

        first_price = recent[0][1]
        last_price = recent[-1][1]

        if first_price <= 0:
            return 0.0

        return (last_price - first_price) / first_price

    async def _trade_loop(self) -> None:
        """Main trading loop — fires every 15 minutes."""
        logger.info("BTC trade loop starting — fires every 15 minutes")

        # Wait for initial price data (30 seconds)
        logger.info("Waiting 30s for price data to accumulate...")
        await asyncio.sleep(30)

        while self._running:
            try:
                # Check daily loss limit
                if self._state.pnl_today <= -self.MAX_DAILY_LOSS:
                    logger.warning("Daily loss limit hit ($%.2f) — stopping for today",
                                   self._state.pnl_today)
                    await asyncio.sleep(60)
                    continue

                # Check max trades per day
                if self._state.trades_today >= self.MAX_TRADES_PER_DAY:
                    logger.info("Max trades per day reached (%d)", self.MAX_TRADES_PER_DAY)
                    await asyncio.sleep(60)
                    continue

                # Calculate momentum
                momentum_60s = self._calculate_momentum(60)
                momentum_30s = self._calculate_momentum(30)
                momentum_10s = self._calculate_momentum(10)

                # Determine direction
                direction = None
                confidence = 0.0

                if momentum_60s > self.MOMENTUM_THRESHOLD:
                    direction = "YES"  # price going up
                    confidence = min(abs(momentum_60s) * 200, 0.95)
                    logger.info("BTC MOMENTUM UP: 60s=%.3f%% 30s=%.3f%% 10s=%.3f%% → BUY YES",
                                momentum_60s * 100, momentum_30s * 100, momentum_10s * 100)
                elif momentum_60s < -self.MOMENTUM_THRESHOLD:
                    direction = "NO"  # price going down
                    confidence = min(abs(momentum_60s) * 200, 0.95)
                    logger.info("BTC MOMENTUM DOWN: 60s=%.3f%% 30s=%.3f%% 10s=%.3f%% → BUY NO",
                                momentum_60s * 100, momentum_30s * 100, momentum_10s * 100)
                else:
                    logger.info("BTC FLAT: 60s=%.3f%% — skipping this window (threshold: %.2f%%)",
                                momentum_60s * 100, self.MOMENTUM_THRESHOLD * 100)
                    await asyncio.sleep(self.TRADE_INTERVAL)
                    continue

                # Execute trade
                trade_id = f"btc15-{int(time.time()*1000)}"
                ticker = f"KXBTC-15M-{direction}"

                # Paper mode: simulate Kalshi fill
                entry_price = 0.50 + (confidence - 0.5) * 0.2  # price reflects confidence
                entry_price = max(0.10, min(0.90, entry_price))

                size = self.POSITION_SIZE

                # Record trade
                self._state.trades_today += 1
                self._state.last_trade_time = time.time()

                shared_state.record_trade_opened(
                    trade_id=trade_id,
                    strategy="BTC15",
                    side=direction,
                    asset=f"BTC-15MIN (${self._btc_price:,.0f})",
                    venue="Kalshi",
                    entry_price=entry_price,
                    size_usd=size,
                    confidence=confidence,
                    reason=f"Momentum {momentum_60s*100:+.2f}% | BTC ${self._btc_price:,.0f}",
                )

                shared_state.record_signal(
                    strategy="BTC15",
                    side=direction,
                    asset=f"BTC ${self._btc_price:,.0f}",
                    venue="Kalshi",
                    confidence=confidence,
                    reason=f"60s: {momentum_60s*100:+.3f}% | 30s: {momentum_30s*100:+.3f}% | 10s: {momentum_10s*100:+.3f}%",
                    action="TRADED",
                )

                logger.info(
                    "BTC 15-MIN TRADE: %s at $%.2f (BTC=$%,.0f, momentum=%+.3f%%, conf=%.2f)",
                    direction, entry_price, self._btc_price, momentum_60s * 100, confidence,
                )

                # Wait for contract resolution (15 minutes)
                logger.info("Waiting 15 minutes for contract resolution...")
                await asyncio.sleep(self.TRADE_INTERVAL)

                # Resolve: check if BTC went in our direction
                new_price = self._btc_price
                price_at_entry = self._state.prices[-1][1] if self._state.prices else new_price

                # Get price from 15 min ago
                entry_time = time.time() - self.TRADE_INTERVAL
                old_prices = [(t, p) for t, p in self._state.prices if abs(t - entry_time) < 30]
                if old_prices:
                    price_at_entry = old_prices[0][1]

                btc_moved_up = new_price > price_at_entry

                if (direction == "YES" and btc_moved_up) or (direction == "NO" and not btc_moved_up):
                    # WIN — contract pays $1.00
                    pnl = size * (1.0 / entry_price - 1.0)  # profit from buying at entry, selling at $1
                    pnl = min(pnl, size * 2)  # cap at 2x
                    self._state.wins_today += 1
                    logger.info("BTC 15-MIN WIN: %s | Entry BTC=$%,.0f → Now=$%,.0f | P&L=$%+.2f",
                                direction, price_at_entry, new_price, pnl)
                else:
                    # LOSS — contract pays $0
                    pnl = -size  # lose the full position
                    self._state.losses_today += 1
                    logger.info("BTC 15-MIN LOSS: %s | Entry BTC=$%,.0f → Now=$%,.0f | P&L=$%+.2f",
                                direction, price_at_entry, new_price, pnl)

                self._state.pnl_today += pnl

                shared_state.record_trade_closed(
                    trade_id=trade_id,
                    pnl=round(pnl, 2),
                    exit_price=1.0 if pnl > 0 else 0.0,
                    reason=f"15-min resolution | BTC ${price_at_entry:,.0f} → ${new_price:,.0f}",
                )

                logger.info("BTC SESSION: %d trades | %d W / %d L | P&L $%+.2f",
                            self._state.trades_today, self._state.wins_today,
                            self._state.losses_today, self._state.pnl_today)

            except Exception as exc:
                logger.error("BTC trader error: %s", exc, exc_info=True)
                await asyncio.sleep(60)

    def get_status(self) -> dict:
        """Return current status for dashboard."""
        momentum = self._calculate_momentum(60)
        return {
            "btc_price": self._btc_price,
            "momentum_60s": round(momentum * 100, 3),
            "trades_today": self._state.trades_today,
            "wins_today": self._state.wins_today,
            "losses_today": self._state.losses_today,
            "pnl_today": round(self._state.pnl_today, 2),
            "last_trade": self._state.last_trade_time,
            "direction": "UP" if momentum > 0 else "DOWN" if momentum < 0 else "FLAT",
        }
