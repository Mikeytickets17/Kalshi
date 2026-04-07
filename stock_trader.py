"""
Stock market execution via Alpaca API.

Trades US stocks and ETFs in response to breaking news:
  - SPY, QQQ for broad market moves
  - Individual stocks for earnings plays
  - TLT for bond/rate plays
  - FXI for China trade war plays

Alpaca: commission-free, no minimum, paper trading built in.
Sign up at https://alpaca.markets
"""

import logging
import time
from dataclasses import dataclass
from typing import Optional

import httpx

import config

logger = logging.getLogger(__name__)


@dataclass
class StockTradeResult:
    success: bool
    order_id: str = ""
    asset: str = ""
    side: str = ""
    filled_price: float = 0.0
    filled_qty: float = 0.0
    filled_usd: float = 0.0
    error: str = ""
    execution_time_ms: float = 0.0


class StockTrader:
    """Executes stock trades via Alpaca API."""

    PAPER_URL = "https://paper-api.alpaca.markets"
    LIVE_URL = "https://api.alpaca.markets"

    def __init__(self) -> None:
        self._api_key = config.ALPACA_API_KEY
        self._secret_key = config.ALPACA_SECRET_KEY
        self._paper_mode = config.PAPER_MODE
        self._base_url = self.PAPER_URL if self._paper_mode else self.LIVE_URL
        self._http = httpx.Client(timeout=10.0)

        if self._api_key:
            logger.info("StockTrader initialized (paper=%s)", self._paper_mode)
        else:
            logger.info("StockTrader: no Alpaca keys, paper fill mode")

    def _headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self._api_key,
            "APCA-API-SECRET-KEY": self._secret_key,
            "Content-Type": "application/json",
        }

    def buy(self, symbol: str, usd_amount: float) -> StockTradeResult:
        return self._execute("buy", symbol, usd_amount)

    def sell(self, symbol: str, usd_amount: float) -> StockTradeResult:
        return self._execute("sell", symbol, usd_amount)

    def _execute(self, side: str, symbol: str, usd_amount: float) -> StockTradeResult:
        start = time.time()

        if not self._api_key:
            return self._paper_fill(side, symbol, usd_amount, start)

        try:
            # Alpaca supports notional (dollar-based) orders
            payload = {
                "symbol": symbol,
                "notional": str(round(usd_amount, 2)),
                "side": side,
                "type": "market",
                "time_in_force": "day",
            }

            resp = self._http.post(
                f"{self._base_url}/v2/orders",
                json=payload,
                headers=self._headers(),
            )
            resp.raise_for_status()
            data = resp.json()

            exec_time = (time.time() - start) * 1000
            logger.info(
                "STOCK %s %s $%.2f (order=%s, time=%dms)",
                side.upper(), symbol, usd_amount, data.get("id", "")[:8], exec_time,
            )

            return StockTradeResult(
                success=True,
                order_id=data.get("id", ""),
                asset=symbol,
                side=side.upper(),
                filled_price=float(data.get("filled_avg_price", 0) or 0),
                filled_qty=float(data.get("filled_qty", 0) or 0),
                filled_usd=usd_amount,
                execution_time_ms=exec_time,
            )
        except Exception as exc:
            logger.error("Alpaca order failed: %s", exc)
            return StockTradeResult(
                success=False, error=str(exc),
                execution_time_ms=(time.time() - start) * 1000,
            )

    def _paper_fill(self, side: str, symbol: str, usd_amount: float, start: float) -> StockTradeResult:
        """Simulate a stock trade."""
        import random
        # Rough price simulation
        prices = {
            "SPY": 520, "QQQ": 440, "AAPL": 195, "MSFT": 420, "GOOGL": 165,
            "AMZN": 185, "TSLA": 175, "NVDA": 880, "META": 500, "TLT": 90,
            "FXI": 28, "DIS": 115, "BA": 180, "JPM": 200, "AMD": 160,
        }
        price = prices.get(symbol, 100) + random.gauss(0, 2)
        qty = usd_amount / price
        exec_time = (time.time() - start) * 1000

        logger.info(
            "[PAPER] STOCK %s %s $%.2f @ $%.2f (qty=%.4f, time=%dms)",
            side.upper(), symbol, usd_amount, price, qty, exec_time,
        )

        return StockTradeResult(
            success=True,
            order_id=f"paper-{int(time.time()*1000)}",
            asset=symbol,
            side=side.upper(),
            filled_price=round(price, 2),
            filled_qty=round(qty, 4),
            filled_usd=round(usd_amount, 2),
            execution_time_ms=exec_time,
        )

    def get_positions(self) -> list[dict]:
        """Get current open positions."""
        if not self._api_key:
            return []
        try:
            resp = self._http.get(f"{self._base_url}/v2/positions", headers=self._headers())
            return resp.json()
        except Exception:
            return []

    def close_position(self, symbol: str) -> bool:
        """Close an entire position in a symbol."""
        if not self._api_key:
            logger.info("[PAPER] Closed stock position: %s", symbol)
            return True
        try:
            self._http.delete(f"{self._base_url}/v2/positions/{symbol}", headers=self._headers())
            return True
        except Exception as exc:
            logger.error("Failed to close %s: %s", symbol, exc)
            return False

    def close(self) -> None:
        self._http.close()
