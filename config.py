"""
Configuration for the Polymarket copy-trading bot.

All parameters are loaded from environment variables with sensible defaults.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# --- Mode ---
PAPER_MODE: bool = os.getenv("PAPER_MODE", "true").lower() == "true"

# --- Wallet Watchlist ---
WATCHLIST_FILE: str = os.getenv("WATCHLIST_FILE", "wallets.json")
MIN_WALLET_WIN_RATE: float = float(os.getenv("MIN_WALLET_WIN_RATE", "0.70"))
WALLET_REFRESH_INTERVAL_HOURS: int = int(os.getenv("WALLET_REFRESH_INTERVAL_HOURS", "6"))
MIN_WALLET_SCORE: float = float(os.getenv("MIN_WALLET_SCORE", "0.65"))

# --- Signal Evaluation ---
MIN_MARKET_LIQUIDITY_USDC: float = float(os.getenv("MIN_MARKET_LIQUIDITY_USDC", "50000"))
MAX_ODDS_SLIPPAGE: float = float(os.getenv("MAX_ODDS_SLIPPAGE", "0.04"))
COPY_THRESHOLD: float = float(os.getenv("COPY_THRESHOLD", "0.60"))
MIN_TIME_REMAINING_SECONDS: int = int(os.getenv("MIN_TIME_REMAINING_SECONDS", "300"))

# --- Position Sizing ---
BASE_COPY_PCT: float = float(os.getenv("BASE_COPY_PCT", "0.03"))
MAX_SINGLE_POSITION_PCT: float = float(os.getenv("MAX_SINGLE_POSITION_PCT", "0.08"))
MIN_TRADE_SIZE_USDC: float = float(os.getenv("MIN_TRADE_SIZE_USDC", "2.0"))
MAX_TRADE_SIZE_USDC: float = float(os.getenv("MAX_TRADE_SIZE_USDC", "1000.0"))
CONVICTION_MULTIPLIER: float = float(os.getenv("CONVICTION_MULTIPLIER", "1.5"))
CONVICTION_THRESHOLD_PCT: float = float(os.getenv("CONVICTION_THRESHOLD_PCT", "0.05"))

# --- Exit Rules ---
STOP_LOSS_PCT: float = float(os.getenv("STOP_LOSS_PCT", "0.50"))
EXIT_TIME_BUFFER_SECONDS: int = int(os.getenv("EXIT_TIME_BUFFER_SECONDS", "60"))

# --- Risk Management ---
DAILY_LOSS_LIMIT_PCT: float = float(os.getenv("DAILY_LOSS_LIMIT_PCT", "0.15"))
DRAWDOWN_KILL_SWITCH_PCT: float = float(os.getenv("DRAWDOWN_KILL_SWITCH_PCT", "0.35"))
MAX_CONCURRENT_POSITIONS: int = int(os.getenv("MAX_CONCURRENT_POSITIONS", "8"))
MAX_CATEGORY_EXPOSURE_PCT: float = float(os.getenv("MAX_CATEGORY_EXPOSURE_PCT", "0.30"))
CONSECUTIVE_LOSSES_KILL: int = int(os.getenv("CONSECUTIVE_LOSSES_KILL", "8"))

# --- Paper Mode Simulation ---
PAPER_DETECTION_DELAY_MIN: float = float(os.getenv("PAPER_DETECTION_DELAY_MIN", "3.0"))
PAPER_DETECTION_DELAY_MAX: float = float(os.getenv("PAPER_DETECTION_DELAY_MAX", "8.0"))
PAPER_INITIAL_BALANCE_USDC: float = float(os.getenv("PAPER_INITIAL_BALANCE_USDC", "10000.0"))

# --- API Keys & Endpoints ---
POLYGON_RPC_WS: str = os.getenv("POLYGON_RPC_WS", "")
ALCHEMY_API_KEY: str = os.getenv("ALCHEMY_API_KEY", "")
DUNE_API_KEY: str = os.getenv("DUNE_API_KEY", "")
POLY_API_KEY: str = os.getenv("POLY_API_KEY", "")
POLY_PRIVATE_KEY: str = os.getenv("POLY_PRIVATE_KEY", "")
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

# --- Polymarket Contract Addresses (Polygon) ---
POLYMARKET_CTF_EXCHANGE: str = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
POLYMARKET_NEG_RISK_EXCHANGE: str = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
USDC_TOKEN_ADDRESS: str = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# --- Polymarket CLOB API ---
POLYMARKET_CLOB_URL: str = os.getenv("POLYMARKET_CLOB_URL", "https://clob.polymarket.com")
POLYMARKET_GAMMA_URL: str = os.getenv("POLYMARKET_GAMMA_URL", "https://gamma-api.polymarket.com")

# --- Logging ---
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE: str = os.getenv("LOG_FILE", "bot.log")

# --- Polling Intervals ---
POSITION_CHECK_INTERVAL_SECONDS: int = int(os.getenv("POSITION_CHECK_INTERVAL_SECONDS", "30"))
EXIT_CHECK_INTERVAL_SECONDS: int = int(os.getenv("EXIT_CHECK_INTERVAL_SECONDS", "15"))
