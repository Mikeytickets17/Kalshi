"""
Compatibility shim — re-exports from kalshi.py.

risk_manager.py, position_sizer.py, and notifier.py import from this module.
All types are defined in kalshi.py; this file simply re-exports them.
"""

from kalshi import (  # noqa: F401
    KalshiClient as PolymarketClient,
    MarketInfo,
    OrderResult,
    Position,
    Side,
    OrderType,
)
