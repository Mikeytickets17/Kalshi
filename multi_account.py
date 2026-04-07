"""
Multi-account Kalshi manager.

Distributes trades across multiple Kalshi accounts to bypass
per-account position limits and maximize capital deployment.

Each account has its own API credentials, balance tracking,
and position limits. The manager routes trades to the best
available account based on:
  1. Available balance
  2. Current position count vs limit
  3. Account-specific risk state

Config via environment:
  KALSHI_ACCOUNTS=account1,account2,account3
  KALSHI_ACCOUNT1_KEY_ID=...
  KALSHI_ACCOUNT1_PRIVATE_KEY_PATH=...
  KALSHI_ACCOUNT2_KEY_ID=...
  ...

Or single account mode (backwards compatible):
  KALSHI_API_KEY_ID=...
  KALSHI_PRIVATE_KEY_PATH=...
"""

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

import config
from kalshi_client import KalshiClient, KalshiMarket, KalshiOrder

logger = logging.getLogger(__name__)


@dataclass
class AccountState:
    """Tracks state for a single Kalshi account."""
    name: str
    client: KalshiClient
    balance: float = 0.0
    positions_open: int = 0
    max_positions: int = 5
    total_trades: int = 0
    total_pnl: float = 0.0
    last_trade_time: float = 0.0
    is_healthy: bool = True
    error_count: int = 0


class MultiAccountManager:
    """Manages multiple Kalshi accounts for trade distribution."""

    def __init__(self) -> None:
        self._accounts: list[AccountState] = []
        self._market_cache: list[KalshiMarket] = []
        self._cache_time: float = 0.0
        self._init_accounts()

    def _init_accounts(self) -> None:
        """Initialize accounts from environment config."""
        # Check for multi-account config
        accounts_str = os.getenv("KALSHI_ACCOUNTS", "")

        if accounts_str:
            # Multi-account mode
            account_names = [a.strip() for a in accounts_str.split(",") if a.strip()]
            for name in account_names:
                prefix = f"KALSHI_{name.upper()}"
                key_id = os.getenv(f"{prefix}_KEY_ID", "")
                key_path = os.getenv(f"{prefix}_PRIVATE_KEY_PATH", "")
                max_pos = int(os.getenv(f"{prefix}_MAX_POSITIONS", "5"))

                if key_id and key_path:
                    # Temporarily override config for this client
                    orig_key = config.KALSHI_API_KEY_ID
                    orig_path = config.KALSHI_PRIVATE_KEY_PATH
                    config.KALSHI_API_KEY_ID = key_id
                    config.KALSHI_PRIVATE_KEY_PATH = key_path

                    client = KalshiClient()

                    config.KALSHI_API_KEY_ID = orig_key
                    config.KALSHI_PRIVATE_KEY_PATH = orig_path

                    self._accounts.append(AccountState(
                        name=name,
                        client=client,
                        max_positions=max_pos,
                    ))
                    logger.info("Loaded Kalshi account: %s (max_pos=%d)", name, max_pos)
                else:
                    logger.warning("Kalshi account %s missing credentials, skipping", name)
        else:
            # Single account mode (backwards compatible)
            client = KalshiClient()
            self._accounts.append(AccountState(
                name="primary",
                client=client,
                max_positions=config.MAX_CONCURRENT_POSITIONS,
            ))
            logger.info("Single Kalshi account mode")

        logger.info("MultiAccountManager: %d accounts loaded", len(self._accounts))

    @property
    def account_count(self) -> int:
        return len(self._accounts)

    @property
    def total_capacity(self) -> int:
        """Total position slots across all accounts."""
        return sum(a.max_positions for a in self._accounts)

    @property
    def available_capacity(self) -> int:
        """How many more positions can be opened across all accounts."""
        return sum(a.max_positions - a.positions_open for a in self._accounts if a.is_healthy)

    def get_best_account(self, size_usd: float = 0) -> Optional[AccountState]:
        """Get the best account to place a trade on."""
        candidates = [
            a for a in self._accounts
            if a.is_healthy
            and a.positions_open < a.max_positions
            and a.error_count < 3
        ]

        if not candidates:
            logger.warning("No healthy accounts with capacity available")
            return None

        # Sort by: fewest positions first, then most recent trade last (spread load)
        candidates.sort(key=lambda a: (a.positions_open, a.last_trade_time))
        return candidates[0]

    def place_order(
        self,
        ticker: str,
        side: str,
        size_usd: float,
        price: float,
    ) -> tuple[Optional[KalshiOrder], str]:
        """Place order on the best available account. Returns (order, account_name)."""
        account = self.get_best_account(size_usd)
        if not account:
            return KalshiOrder(success=False, error="No accounts available"), ""

        order = account.client.place_order(ticker, side, size_usd, price)

        if order.success:
            account.positions_open += 1
            account.total_trades += 1
            account.last_trade_time = time.time()
            account.error_count = 0
            logger.info(
                "Order placed on account '%s': %s %s $%.2f @ %.4f (pos %d/%d)",
                account.name, side, ticker, size_usd, price,
                account.positions_open, account.max_positions,
            )
        else:
            account.error_count += 1
            if account.error_count >= 3:
                account.is_healthy = False
                logger.error("Account '%s' marked unhealthy after 3 errors", account.name)

        return order, account.name

    def close_position(self, account_name: str) -> None:
        """Record that a position was closed on an account."""
        for account in self._accounts:
            if account.name == account_name:
                account.positions_open = max(0, account.positions_open - 1)
                break

    def record_pnl(self, account_name: str, pnl: float) -> None:
        """Record P&L for an account."""
        for account in self._accounts:
            if account.name == account_name:
                account.total_pnl += pnl
                break

    def get_markets(self) -> list[KalshiMarket]:
        """Get markets from the first connected account (shared across all)."""
        if time.time() - self._cache_time > 60:
            for account in self._accounts:
                if account.client.is_connected:
                    self._market_cache = account.client.get_crypto_markets()
                    self._cache_time = time.time()
                    break
        return self._market_cache

    def get_status(self) -> list[dict]:
        """Get status of all accounts."""
        return [
            {
                "name": a.name,
                "connected": a.client.is_connected,
                "healthy": a.is_healthy,
                "positions": f"{a.positions_open}/{a.max_positions}",
                "trades": a.total_trades,
                "pnl": round(a.total_pnl, 2),
                "errors": a.error_count,
            }
            for a in self._accounts
        ]

    def cancel_all(self) -> None:
        """Cancel all orders on all accounts."""
        for account in self._accounts:
            account.client.cancel_all()

    def close(self) -> None:
        """Close all account connections."""
        for account in self._accounts:
            account.client.close()
