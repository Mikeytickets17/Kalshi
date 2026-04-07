"""
Order book + order flow analysis.

Reads real-time order books from Binance and tracks:
  - Bid/ask depth (where the walls are)
  - Order flow imbalance (more buying or selling pressure?)
  - Large order detection (institutional flow)
  - Price reaction speed after news (priced in or not?)

The bot NEVER trades on news alone. It requires:
  1. News drops
  2. Wait 1-3 seconds
  3. Confirm price is ACTUALLY moving (not priced in)
  4. Confirm order flow agrees with our direction
  5. Confirm order book has liquidity to fill us
  THEN trade.
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
class OrderBookSnapshot:
    """Current state of an order book."""
    asset: str
    timestamp: float
    best_bid: float = 0.0
    best_ask: float = 0.0
    bid_depth_10: float = 0.0   # Total USD within 0.1% of best bid
    ask_depth_10: float = 0.0   # Total USD within 0.1% of best ask
    bid_depth_50: float = 0.0   # Total USD within 0.5% of best bid
    ask_depth_50: float = 0.0   # Total USD within 0.5% of best ask
    spread_pct: float = 0.0     # Bid-ask spread as percentage
    imbalance: float = 0.0      # -1 (all sell) to +1 (all buy)

    @property
    def mid_price(self) -> float:
        return (self.best_bid + self.best_ask) / 2 if self.best_bid and self.best_ask else 0


@dataclass
class OrderFlowSignal:
    """Detected order flow pattern."""
    asset: str
    direction: str              # "aggressive_buy", "aggressive_sell", "neutral"
    strength: float             # 0.0 to 1.0
    imbalance: float            # -1 to +1
    large_orders_side: str      # "buy", "sell", "none" — where the big orders are
    volume_surge: bool          # Is volume spiking right now?
    price_moving: bool          # Is price actually trending, not just noise?
    price_change_pct: float     # Price change in the analysis window
    priced_in: bool             # Is the move already done?
    timestamp: float = field(default_factory=time.time)


@dataclass
class TradeDecision:
    """Final decision: should we trade, which side, and why."""
    should_trade: bool
    side: str                   # "BUY" or "SELL"
    asset: str
    confidence: float
    reason: str
    entry_price: float
    book_support: bool          # Does the order book support this trade?
    flow_confirms: bool         # Does order flow confirm the direction?
    not_priced_in: bool         # Is the move NOT already priced in?
    size_suggestion: float      # Suggested size based on book depth


class OrderBookReader:
    """Reads and analyzes order books from Binance."""

    def __init__(self) -> None:
        self._running = False
        self._books: dict[str, OrderBookSnapshot] = {}
        self._trade_flow: dict[str, deque] = {}  # Recent trades for flow analysis
        self._price_snapshots: dict[str, deque] = {}  # For reaction detection

        for asset in config.TARGET_ASSETS:
            symbol = f"{asset}USDT"
            self._books[symbol] = OrderBookSnapshot(asset=asset, timestamp=0)
            self._trade_flow[symbol] = deque(maxlen=500)
            self._price_snapshots[symbol] = deque(maxlen=100)

    def get_book(self, asset: str) -> Optional[OrderBookSnapshot]:
        symbol = f"{asset}USDT"
        book = self._books.get(symbol)
        if book and book.timestamp > 0:
            return book
        return None

    async def start(self) -> None:
        """Start streaming order book data."""
        self._running = True
        logger.info("OrderBookReader starting for %s", config.TARGET_ASSETS)

        if config.PAPER_MODE:
            await self._run_paper_mode()
        else:
            tasks = []
            for asset in config.TARGET_ASSETS:
                symbol = f"{asset.lower()}usdt"
                tasks.append(asyncio.create_task(
                    self._stream_book(symbol),
                    name=f"book_{symbol}"
                ))
                tasks.append(asyncio.create_task(
                    self._stream_trades(symbol),
                    name=f"flow_{symbol}"
                ))
            await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)

    async def stop(self) -> None:
        self._running = False

    async def _stream_book(self, symbol: str) -> None:
        """Stream order book depth from Binance WebSocket."""
        import websockets
        url = f"wss://stream.binance.com:9443/ws/{symbol}@depth20@100ms"

        while self._running:
            try:
                async with websockets.connect(url, ping_interval=20) as ws:
                    while self._running:
                        msg = await asyncio.wait_for(ws.recv(), timeout=30)
                        data = json.loads(msg)
                        self._update_book(symbol.upper(), data)
            except Exception as exc:
                logger.debug("Book stream %s error: %s", symbol, exc)
                await asyncio.sleep(2)

    def _update_book(self, symbol: str, data: dict) -> None:
        """Parse depth update into OrderBookSnapshot."""
        bids = data.get("bids", [])
        asks = data.get("asks", [])

        if not bids or not asks:
            return

        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
        mid = (best_bid + best_ask) / 2

        # Calculate depth at different levels
        bid_10 = sum(float(b[0]) * float(b[1]) for b in bids if float(b[0]) >= mid * 0.999)
        ask_10 = sum(float(a[0]) * float(a[1]) for a in asks if float(a[0]) <= mid * 1.001)
        bid_50 = sum(float(b[0]) * float(b[1]) for b in bids if float(b[0]) >= mid * 0.995)
        ask_50 = sum(float(a[0]) * float(a[1]) for a in asks if float(a[0]) <= mid * 1.005)

        total_near = bid_10 + ask_10
        imbalance = (bid_10 - ask_10) / total_near if total_near > 0 else 0

        self._books[symbol] = OrderBookSnapshot(
            asset=symbol.replace("USDT", ""),
            timestamp=time.time(),
            best_bid=best_bid,
            best_ask=best_ask,
            bid_depth_10=bid_10,
            ask_depth_10=ask_10,
            bid_depth_50=bid_50,
            ask_depth_50=ask_50,
            spread_pct=(best_ask - best_bid) / mid * 100 if mid > 0 else 0,
            imbalance=round(imbalance, 4),
        )

    async def _stream_trades(self, symbol: str) -> None:
        """Stream individual trades for flow analysis."""
        import websockets
        url = f"wss://stream.binance.com:9443/ws/{symbol}@aggTrade"

        while self._running:
            try:
                async with websockets.connect(url, ping_interval=20) as ws:
                    while self._running:
                        msg = await asyncio.wait_for(ws.recv(), timeout=30)
                        data = json.loads(msg)
                        trade = {
                            "price": float(data["p"]),
                            "qty": float(data["q"]),
                            "usd": float(data["p"]) * float(data["q"]),
                            "side": "sell" if data.get("m", False) else "buy",
                            "ts": float(data["T"]) / 1000,
                        }
                        self._trade_flow[symbol.upper()].append(trade)
                        self._price_snapshots[symbol.upper()].append({
                            "price": trade["price"],
                            "ts": trade["ts"],
                        })
            except Exception as exc:
                logger.debug("Trade stream %s error: %s", symbol, exc)
                await asyncio.sleep(2)

    def analyze_flow(self, asset: str, window_seconds: float = 3.0) -> OrderFlowSignal:
        """
        Analyze recent order flow to determine market direction.

        Looks at the last N seconds of trades to determine:
        - Are buyers or sellers more aggressive?
        - Are there large orders on one side?
        - Is volume spiking (news reaction)?
        - Is price actually moving or just noise?
        """
        symbol = f"{asset}USDT"
        trades = list(self._trade_flow.get(symbol, []))
        prices = list(self._price_snapshots.get(symbol, []))

        now = time.time()
        recent = [t for t in trades if now - t["ts"] < window_seconds]

        if not recent:
            return OrderFlowSignal(
                asset=asset, direction="neutral", strength=0,
                imbalance=0, large_orders_side="none",
                volume_surge=False, price_moving=False,
                price_change_pct=0, priced_in=True,
            )

        # Buy vs sell volume
        buy_vol = sum(t["usd"] for t in recent if t["side"] == "buy")
        sell_vol = sum(t["usd"] for t in recent if t["side"] == "sell")
        total_vol = buy_vol + sell_vol
        imbalance = (buy_vol - sell_vol) / total_vol if total_vol > 0 else 0

        # Large order detection (>$50k single trade)
        large_buys = [t for t in recent if t["side"] == "buy" and t["usd"] > 50000]
        large_sells = [t for t in recent if t["side"] == "sell" and t["usd"] > 50000]
        if large_buys and not large_sells:
            large_side = "buy"
        elif large_sells and not large_buys:
            large_side = "sell"
        elif large_buys and large_sells:
            large_side = "buy" if sum(t["usd"] for t in large_buys) > sum(t["usd"] for t in large_sells) else "sell"
        else:
            large_side = "none"

        # Volume surge detection (compare to baseline)
        baseline_trades = [t for t in trades if now - t["ts"] < 30 and now - t["ts"] >= window_seconds]
        baseline_vol = sum(t["usd"] for t in baseline_trades) / max(30 - window_seconds, 1) * window_seconds
        volume_surge = total_vol > baseline_vol * 2.5 if baseline_vol > 0 else total_vol > 100000

        # Price movement
        recent_prices = [p for p in prices if now - p["ts"] < window_seconds]
        if len(recent_prices) >= 2:
            first_price = recent_prices[0]["price"]
            last_price = recent_prices[-1]["price"]
            price_change = (last_price - first_price) / first_price
            price_moving = abs(price_change) > 0.001  # >0.1% move
        else:
            price_change = 0
            price_moving = False

        # Priced in detection: if we saw a move in the PREVIOUS window
        # but not the current one, it's already priced in
        prev_prices = [p for p in prices if now - p["ts"] < window_seconds * 3 and now - p["ts"] >= window_seconds]
        if prev_prices and recent_prices:
            prev_move = abs(prev_prices[-1]["price"] - prev_prices[0]["price"]) / prev_prices[0]["price"] if prev_prices[0]["price"] > 0 else 0
            curr_move = abs(price_change)
            priced_in = prev_move > 0.002 and curr_move < 0.0005  # Big move before, flat now
        else:
            priced_in = False

        # Direction
        if imbalance > 0.3 and price_moving and price_change > 0:
            direction = "aggressive_buy"
            strength = min(abs(imbalance), 1.0)
        elif imbalance < -0.3 and price_moving and price_change < 0:
            direction = "aggressive_sell"
            strength = min(abs(imbalance), 1.0)
        else:
            direction = "neutral"
            strength = 0

        return OrderFlowSignal(
            asset=asset,
            direction=direction,
            strength=round(strength, 3),
            imbalance=round(imbalance, 4),
            large_orders_side=large_side,
            volume_surge=volume_surge,
            price_moving=price_moving,
            price_change_pct=round(price_change * 100, 4),
            priced_in=priced_in,
        )

    def make_decision(
        self, asset: str, news_direction: str, news_confidence: float
    ) -> TradeDecision:
        """
        Final trade decision combining news + order book + order flow.

        Rules:
        1. If order flow DISAGREES with news direction → DON'T TRADE
           (market knows something we don't)
        2. If price isn't moving after news → DON'T TRADE (priced in)
        3. If order book is thin on our side → REDUCE SIZE
        4. If flow CONFIRMS news + price moving + volume surge → FULL SEND
        """
        flow = self.analyze_flow(asset, window_seconds=3.0)
        book = self.get_book(asset)

        # Default: don't trade
        decision = TradeDecision(
            should_trade=False, side="", asset=asset,
            confidence=0, reason="",
            entry_price=book.mid_price if book else 0,
            book_support=False, flow_confirms=False,
            not_priced_in=False, size_suggestion=0,
        )

        # Rule 1: Is it priced in?
        if flow.priced_in:
            decision.reason = "SKIP: News already priced in — price moved before, flat now"
            return decision

        # Rule 2: Is price actually moving?
        if not flow.price_moving and not flow.volume_surge:
            decision.reason = "SKIP: No price reaction to news — market doesn't care"
            return decision

        # Rule 3: Does order flow agree with news?
        news_is_bullish = news_direction in ("bullish", "BUY", "LONG", "YES")

        if news_is_bullish and flow.direction == "aggressive_sell":
            decision.reason = "SKIP: News says buy but market is aggressively selling — flow disagrees"
            return decision
        if not news_is_bullish and flow.direction == "aggressive_buy":
            decision.reason = "SKIP: News says sell but market is aggressively buying — flow disagrees"
            return decision

        # Rule 4: All signals align — TRADE
        flow_confirms = (
            (news_is_bullish and flow.direction == "aggressive_buy") or
            (not news_is_bullish and flow.direction == "aggressive_sell")
        )

        side = "BUY" if news_is_bullish else "SELL"

        # Size based on order book depth (don't exceed 5% of near liquidity)
        if book:
            available_liquidity = book.bid_depth_50 if side == "SELL" else book.ask_depth_50
            max_size_from_book = available_liquidity * 0.05
        else:
            max_size_from_book = 500

        # Confidence boost if everything aligns
        conf = news_confidence
        if flow_confirms:
            conf = min(conf + 0.15, 0.98)
        if flow.volume_surge:
            conf = min(conf + 0.10, 0.98)
        if flow.large_orders_side == ("buy" if news_is_bullish else "sell"):
            conf = min(conf + 0.10, 0.98)

        reason_parts = []
        if flow_confirms:
            reason_parts.append(f"flow confirms ({flow.direction})")
        if flow.volume_surge:
            reason_parts.append("volume surge detected")
        if flow.large_orders_side != "none":
            reason_parts.append(f"large orders on {flow.large_orders_side} side")
        reason_parts.append(f"price moved {flow.price_change_pct:+.2f}%")
        reason_parts.append(f"imbalance {flow.imbalance:+.2f}")

        decision.should_trade = True
        decision.side = side
        decision.confidence = round(conf, 3)
        decision.reason = "TRADE: " + " | ".join(reason_parts)
        decision.entry_price = book.mid_price if book else 0
        decision.book_support = book is not None and max_size_from_book > 50
        decision.flow_confirms = flow_confirms
        decision.not_priced_in = not flow.priced_in
        decision.size_suggestion = min(max_size_from_book, 500)

        return decision

    # --- Paper Mode ---

    async def _run_paper_mode(self) -> None:
        """Simulate order book data in paper mode."""
        import random
        logger.info("[PAPER] OrderBookReader running in simulation mode")

        while self._running:
            for asset in config.TARGET_ASSETS:
                symbol = f"{asset}USDT"
                base = {"BTC": 83500, "ETH": 1800}.get(asset, 50000)
                mid = base + random.gauss(0, base * 0.0002)

                self._books[symbol] = OrderBookSnapshot(
                    asset=asset, timestamp=time.time(),
                    best_bid=mid * 0.9999, best_ask=mid * 1.0001,
                    bid_depth_10=random.uniform(500000, 2000000),
                    ask_depth_10=random.uniform(500000, 2000000),
                    bid_depth_50=random.uniform(2000000, 8000000),
                    ask_depth_50=random.uniform(2000000, 8000000),
                    spread_pct=round(random.uniform(0.001, 0.005), 4),
                    imbalance=round(random.gauss(0, 0.3), 4),
                )

                # Simulate trade flow
                for _ in range(random.randint(5, 20)):
                    self._trade_flow[symbol].append({
                        "price": mid + random.gauss(0, mid * 0.0001),
                        "qty": random.uniform(0.001, 0.5),
                        "usd": random.uniform(50, 30000),
                        "side": random.choice(["buy", "sell"]),
                        "ts": time.time() - random.uniform(0, 5),
                    })
                    self._price_snapshots[symbol].append({
                        "price": mid + random.gauss(0, mid * 0.0001),
                        "ts": time.time() - random.uniform(0, 5),
                    })

            await asyncio.sleep(0.5)
