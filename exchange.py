"""
Exchange execution module — Binance spot BTC/ETH trading.

Places market orders on Binance for instant execution when a
Trump post moves the market. Also supports Coinbase as backup.

Speed target: order placed within 200ms of decision.
"""

import hashlib
import hmac
import logging
import time
import urllib.parse
from dataclasses import dataclass
from typing import Optional

import httpx

import config

logger = logging.getLogger(__name__)


@dataclass
class TradeResult:
    """Result of a trade execution."""
    success: bool
    order_id: str = ""
    side: str = ""           # "BUY" or "SELL"
    asset: str = ""          # "BTC", "ETH"
    filled_price: float = 0.0
    filled_qty: float = 0.0
    filled_usd: float = 0.0
    error: str = ""
    execution_time_ms: float = 0.0


class BinanceExecutor:
    """Executes spot trades on Binance."""

    BASE_URL = "https://api.binance.com"

    def __init__(self) -> None:
        self._api_key = config.BINANCE_API_KEY
        self._secret_key = config.BINANCE_SECRET_KEY
        self._paper_mode = config.PAPER_MODE
        self._http = httpx.Client(timeout=5.0)

        if self._api_key and self._secret_key:
            logger.info("BinanceExecutor initialized (paper=%s)", self._paper_mode)
        else:
            logger.info("BinanceExecutor: no API keys, paper mode only")

    def buy(self, asset: str, usdc_amount: float) -> TradeResult:
        """Buy spot BTC/ETH with USDC. Market order for speed."""
        symbol = f"{asset}USDT"
        return self._execute("BUY", symbol, asset, usdc_amount)

    def sell(self, asset: str, usdc_amount: float) -> TradeResult:
        """Sell spot BTC/ETH for USDC. Market order for speed."""
        symbol = f"{asset}USDT"
        return self._execute("SELL", symbol, asset, usdc_amount)

    def _execute(self, side: str, symbol: str, asset: str, usdc_amount: float) -> TradeResult:
        start = time.time()

        if self._paper_mode:
            return self._paper_fill(side, symbol, asset, usdc_amount, start)

        if not self._api_key:
            return TradeResult(success=False, error="No Binance API keys configured")

        try:
            # Get current price for quantity calculation
            ticker = self._http.get(f"{self.BASE_URL}/api/v3/ticker/price", params={"symbol": symbol})
            current_price = float(ticker.json()["price"])

            if side == "BUY":
                # quoteOrderQty = spend this many USDT
                params = {
                    "symbol": symbol,
                    "side": "BUY",
                    "type": "MARKET",
                    "quoteOrderQty": f"{usdc_amount:.2f}",
                    "timestamp": str(int(time.time() * 1000)),
                }
            else:
                # Sell a quantity of the asset
                qty = usdc_amount / current_price
                params = {
                    "symbol": symbol,
                    "side": "SELL",
                    "type": "MARKET",
                    "quantity": f"{qty:.6f}",
                    "timestamp": str(int(time.time() * 1000)),
                }

            # Sign the request
            query_string = urllib.parse.urlencode(params)
            signature = hmac.new(
                self._secret_key.encode(), query_string.encode(), hashlib.sha256
            ).hexdigest()
            params["signature"] = signature

            resp = self._http.post(
                f"{self.BASE_URL}/api/v3/order",
                params=params,
                headers={"X-MBX-APIKEY": self._api_key},
            )
            resp.raise_for_status()
            data = resp.json()

            # Parse fills
            fills = data.get("fills", [])
            total_qty = sum(float(f["qty"]) for f in fills)
            total_cost = sum(float(f["qty"]) * float(f["price"]) for f in fills)
            avg_price = total_cost / total_qty if total_qty > 0 else current_price

            exec_time = (time.time() - start) * 1000

            logger.info(
                "EXECUTED: %s %s $%.2f @ $%.2f (qty=%.6f, time=%dms)",
                side, asset, total_cost, avg_price, total_qty, exec_time,
            )

            return TradeResult(
                success=True,
                order_id=str(data.get("orderId", "")),
                side=side,
                asset=asset,
                filled_price=avg_price,
                filled_qty=total_qty,
                filled_usd=total_cost,
                execution_time_ms=exec_time,
            )

        except Exception as exc:
            logger.error("Binance order failed: %s", exc)
            return TradeResult(
                success=False, error=str(exc),
                execution_time_ms=(time.time() - start) * 1000,
            )

    def _paper_fill(self, side: str, symbol: str, asset: str, usdc_amount: float, start: float) -> TradeResult:
        """Simulate a fill in paper mode."""
        # Use a realistic simulated price
        from price_feed import PriceFeed
        import random

        # Rough price simulation
        prices = {"BTC": 68500, "ETH": 3450}
        price = prices.get(asset, 50000) + random.gauss(0, prices.get(asset, 50000) * 0.001)

        slippage = 0.001  # 0.1% slippage on market order
        if side == "BUY":
            fill_price = price * (1 + slippage)
        else:
            fill_price = price * (1 - slippage)

        qty = usdc_amount / fill_price
        exec_time = (time.time() - start) * 1000

        logger.info(
            "[PAPER] %s %s $%.2f @ $%.2f (qty=%.6f, time=%dms)",
            side, asset, usdc_amount, fill_price, qty, exec_time,
        )

        return TradeResult(
            success=True,
            order_id=f"paper-{int(time.time()*1000)}",
            side=side,
            asset=asset,
            filled_price=round(fill_price, 2),
            filled_qty=round(qty, 6),
            filled_usd=round(usdc_amount, 2),
            execution_time_ms=exec_time,
        )

    def get_balance(self, asset: str = "USDT") -> float:
        """Get available balance for an asset."""
        if self._paper_mode:
            return config.PAPER_INITIAL_BALANCE_USDC
        try:
            params = {"timestamp": str(int(time.time() * 1000))}
            query = urllib.parse.urlencode(params)
            sig = hmac.new(self._secret_key.encode(), query.encode(), hashlib.sha256).hexdigest()
            params["signature"] = sig

            resp = self._http.get(
                f"{self.BASE_URL}/api/v3/account",
                params=params,
                headers={"X-MBX-APIKEY": self._api_key},
            )
            resp.raise_for_status()
            for bal in resp.json().get("balances", []):
                if bal["asset"] == asset:
                    return float(bal["free"])
            return 0.0
        except Exception as exc:
            logger.error("Failed to get balance: %s", exc)
            return 0.0

    def close(self) -> None:
        self._http.close()
