"""
Configuration for the Kalshi longshot bias trading bot.

All parameters are loaded from environment variables with sensible defaults.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# --- Mode ---
PAPER_MODE: bool = os.getenv("PAPER_MODE", "true").lower() == "true"

# --- Kalshi API ---
KALSHI_API_KEY_ID: str = os.getenv("KALSHI_API_KEY_ID", "")
KALSHI_PRIVATE_KEY_PATH: str = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")
KALSHI_USE_DEMO: bool = os.getenv("KALSHI_USE_DEMO", "true").lower() == "true"

# --- Market Scanning ---
SCAN_INTERVAL_SECONDS: int = int(os.getenv("SCAN_INTERVAL_SECONDS", "120"))
MIN_MARKET_VOLUME: float = float(os.getenv("MIN_MARKET_VOLUME", "5000"))

# Longshot fade: YES contracts below this price, ANY category
# Only trade longshots cheap enough that the bias overcomes fees
LONGSHOT_MAX_PRICE: float = float(os.getenv("LONGSHOT_MAX_PRICE", "0.12"))
LONGSHOT_MIN_EDGE: float = float(os.getenv("LONGSHOT_MIN_EDGE", "0.03"))

# Favorite lean: YES contracts above this price, ANY category
FAVORITE_MIN_PRICE: float = float(os.getenv("FAVORITE_MIN_PRICE", "0.75"))
FAVORITE_MIN_EDGE: float = float(os.getenv("FAVORITE_MIN_EDGE", "0.02"))

# --- Signal Evaluation ---
SIGNAL_THRESHOLD: float = float(os.getenv("SIGNAL_THRESHOLD", "0.50"))
MIN_TIME_REMAINING_SECONDS: int = int(os.getenv("MIN_TIME_REMAINING_SECONDS", "600"))

# --- Position Sizing ---
BASE_COPY_PCT: float = float(os.getenv("BASE_COPY_PCT", "0.03"))
MAX_SINGLE_POSITION_PCT: float = float(os.getenv("MAX_SINGLE_POSITION_PCT", "0.08"))
MIN_TRADE_SIZE_USDC: float = float(os.getenv("MIN_TRADE_SIZE_USDC", "2.0"))
MAX_TRADE_SIZE_USDC: float = float(os.getenv("MAX_TRADE_SIZE_USDC", "500.0"))
CONVICTION_MULTIPLIER: float = float(os.getenv("CONVICTION_MULTIPLIER", "1.5"))
CONVICTION_THRESHOLD_PCT: float = float(os.getenv("CONVICTION_THRESHOLD_PCT", "0.05"))

# --- Exit Rules ---
STOP_LOSS_PCT: float = float(os.getenv("STOP_LOSS_PCT", "0.50"))
EXIT_TIME_BUFFER_SECONDS: int = int(os.getenv("EXIT_TIME_BUFFER_SECONDS", "60"))

# --- Risk Management ---
DAILY_LOSS_LIMIT_PCT: float = float(os.getenv("DAILY_LOSS_LIMIT_PCT", "0.10"))
DRAWDOWN_KILL_SWITCH_PCT: float = float(os.getenv("DRAWDOWN_KILL_SWITCH_PCT", "0.25"))
MAX_CONCURRENT_POSITIONS: int = int(os.getenv("MAX_CONCURRENT_POSITIONS", "15"))
MAX_CATEGORY_EXPOSURE_PCT: float = float(os.getenv("MAX_CATEGORY_EXPOSURE_PCT", "0.35"))
CONSECUTIVE_LOSSES_KILL: int = int(os.getenv("CONSECUTIVE_LOSSES_KILL", "8"))
WALLET_COOLDOWN_TRADES: int = int(os.getenv("WALLET_COOLDOWN_TRADES", "20"))
WALLET_PAUSE_MIN_TRADES: int = int(os.getenv("WALLET_PAUSE_MIN_TRADES", "10"))
WALLET_PAUSE_WR_THRESHOLD: float = float(os.getenv("WALLET_PAUSE_WR_THRESHOLD", "0.50"))
WALLET_PAUSE_CONSEC_LOSSES: int = int(os.getenv("WALLET_PAUSE_CONSEC_LOSSES", "4"))
CONVERGENCE_WINDOW_SECONDS: int = int(os.getenv("CONVERGENCE_WINDOW_SECONDS", "3600"))

# --- Paper Mode Simulation ---
PAPER_INITIAL_BALANCE_USDC: float = float(os.getenv("PAPER_INITIAL_BALANCE_USDC", "10000.0"))

# --- Telegram Notifications ---
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

# --- Logging ---
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE: str = os.getenv("LOG_FILE", "bot.log")

# --- Polling Intervals ---
POSITION_CHECK_INTERVAL_SECONDS: int = int(os.getenv("POSITION_CHECK_INTERVAL_SECONDS", "30"))
EXIT_CHECK_INTERVAL_SECONDS: int = int(os.getenv("EXIT_CHECK_INTERVAL_SECONDS", "15"))
