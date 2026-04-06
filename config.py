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
SCAN_INTERVAL_SECONDS: int = int(os.getenv("SCAN_INTERVAL_SECONDS", "60"))
FAST_SCAN_INTERVAL_SECONDS: int = int(os.getenv("FAST_SCAN_INTERVAL_SECONDS", "20"))
MIN_MARKET_VOLUME: float = float(os.getenv("MIN_MARKET_VOLUME", "500"))
LONGSHOT_MAX_PRICE: float = float(os.getenv("LONGSHOT_MAX_PRICE", "0.25"))
FAVORITE_MIN_PRICE: float = float(os.getenv("FAVORITE_MIN_PRICE", "0.65"))
MIDRANGE_LOW: float = float(os.getenv("MIDRANGE_LOW", "0.25"))
MIDRANGE_HIGH: float = float(os.getenv("MIDRANGE_HIGH", "0.65"))
CLOSING_SOON_HOURS: int = int(os.getenv("CLOSING_SOON_HOURS", "6"))

# --- Signal Evaluation ---
SIGNAL_THRESHOLD: float = float(os.getenv("SIGNAL_THRESHOLD", "0.45"))
MIN_TIME_REMAINING_SECONDS: int = int(os.getenv("MIN_TIME_REMAINING_SECONDS", "120"))

# --- Position Sizing ---
BASE_COPY_PCT: float = float(os.getenv("BASE_COPY_PCT", "0.04"))
MAX_SINGLE_POSITION_PCT: float = float(os.getenv("MAX_SINGLE_POSITION_PCT", "0.10"))
MIN_TRADE_SIZE_USDC: float = float(os.getenv("MIN_TRADE_SIZE_USDC", "1.0"))
MAX_TRADE_SIZE_USDC: float = float(os.getenv("MAX_TRADE_SIZE_USDC", "1000.0"))
CONVICTION_MULTIPLIER: float = float(os.getenv("CONVICTION_MULTIPLIER", "1.5"))
CONVICTION_THRESHOLD_PCT: float = float(os.getenv("CONVICTION_THRESHOLD_PCT", "0.05"))

# --- Exit Rules ---
STOP_LOSS_PCT: float = float(os.getenv("STOP_LOSS_PCT", "0.50"))
EXIT_TIME_BUFFER_SECONDS: int = int(os.getenv("EXIT_TIME_BUFFER_SECONDS", "60"))

# --- Risk Management ---
DAILY_LOSS_LIMIT_PCT: float = float(os.getenv("DAILY_LOSS_LIMIT_PCT", "0.15"))
DRAWDOWN_KILL_SWITCH_PCT: float = float(os.getenv("DRAWDOWN_KILL_SWITCH_PCT", "0.35"))
MAX_CONCURRENT_POSITIONS: int = int(os.getenv("MAX_CONCURRENT_POSITIONS", "25"))
MAX_CATEGORY_EXPOSURE_PCT: float = float(os.getenv("MAX_CATEGORY_EXPOSURE_PCT", "0.40"))
CONSECUTIVE_LOSSES_KILL: int = int(os.getenv("CONSECUTIVE_LOSSES_KILL", "12"))
WALLET_COOLDOWN_TRADES: int = int(os.getenv("WALLET_COOLDOWN_TRADES", "20"))
WALLET_PAUSE_MIN_TRADES: int = int(os.getenv("WALLET_PAUSE_MIN_TRADES", "10"))
WALLET_PAUSE_WR_THRESHOLD: float = float(os.getenv("WALLET_PAUSE_WR_THRESHOLD", "0.45"))
WALLET_PAUSE_CONSEC_LOSSES: int = int(os.getenv("WALLET_PAUSE_CONSEC_LOSSES", "6"))
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
