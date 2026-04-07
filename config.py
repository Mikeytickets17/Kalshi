"""
Configuration for the Kalshi Latency Arb + Trump News Trading Bot.

Two strategies running from New Jersey, both US-legal:
  1. Latency arb on Kalshi crypto contracts
  2. Trump Truth Social → Claude → Binance BTC spot
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
KALSHI_BASE_URL_DEMO: str = "https://demo-api.kalshi.co/trade-api/v2"
KALSHI_BASE_URL_PROD: str = "https://api.elections.kalshi.com/trade-api/v2"

# --- Alpaca Stock Trading ---
ALPACA_API_KEY: str = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY: str = os.getenv("ALPACA_SECRET_KEY", "")

# --- Binance Spot Trading ---
BINANCE_API_KEY: str = os.getenv("BINANCE_API_KEY", "")
BINANCE_SECRET_KEY: str = os.getenv("BINANCE_SECRET_KEY", "")

# --- CEX Price Feeds (for latency arb) ---
BINANCE_WS_URL: str = os.getenv("BINANCE_WS_URL", "wss://stream.binance.com:9443/ws")
COINBASE_WS_URL: str = os.getenv("COINBASE_WS_URL", "wss://ws-feed.exchange.coinbase.com")

# --- Trump Monitor ---
TRUMP_POLL_INTERVAL_SECONDS: float = float(os.getenv("TRUMP_POLL_INTERVAL_SECONDS", "3.0"))
TRUMP_MIN_CONFIDENCE: float = float(os.getenv("TRUMP_MIN_CONFIDENCE", "0.45"))
TRUMP_TRADE_SIZE_PCT: float = float(os.getenv("TRUMP_TRADE_SIZE_PCT", "0.06"))
TRUMP_MAX_TRADE_SIZE_USDC: float = float(os.getenv("TRUMP_MAX_TRADE_SIZE_USDC", "750.0"))
TRUMP_HOLD_MINUTES: int = int(os.getenv("TRUMP_HOLD_MINUTES", "20"))
TRUMP_SCALE_BY_CONFIDENCE: bool = os.getenv("TRUMP_SCALE_BY_CONFIDENCE", "true").lower() == "true"

# --- Claude API (for sentiment analysis) ---
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")

# --- Edge Detection (Kalshi latency arb) ---
EDGE_THRESHOLD_PCT: float = float(os.getenv("EDGE_THRESHOLD_PCT", "0.03"))
MAX_EDGE_PCT: float = float(os.getenv("MAX_EDGE_PCT", "0.15"))
CONFIRMATION_WINDOW_MS: int = int(os.getenv("CONFIRMATION_WINDOW_MS", "500"))
MIN_CONTRACT_DURATION_SECONDS: int = int(os.getenv("MIN_CONTRACT_DURATION_SECONDS", "60"))

# --- Target Markets ---
TARGET_ASSETS: list[str] = [x.strip() for x in os.getenv("TARGET_ASSETS", "BTC,ETH").split(",")]
TARGET_DURATIONS: list[int] = [int(x.strip()) for x in os.getenv("TARGET_DURATIONS", "15,60").split(",")]

# --- Position Sizing ---
BASE_COPY_PCT: float = float(os.getenv("BASE_COPY_PCT", "0.05"))
MAX_SINGLE_POSITION_PCT: float = float(os.getenv("MAX_SINGLE_POSITION_PCT", "0.08"))
MIN_TRADE_SIZE_USDC: float = float(os.getenv("MIN_TRADE_SIZE_USDC", "1.0"))
MAX_TRADE_SIZE_USDC: float = float(os.getenv("MAX_TRADE_SIZE_USDC", "500.0"))
CONVICTION_MULTIPLIER: float = float(os.getenv("CONVICTION_MULTIPLIER", "1.5"))
CONVICTION_THRESHOLD_PCT: float = float(os.getenv("CONVICTION_THRESHOLD_PCT", "0.05"))

# --- Exit Rules ---
STOP_LOSS_PCT: float = float(os.getenv("STOP_LOSS_PCT", "0.50"))
EXIT_TIME_BUFFER_SECONDS: int = int(os.getenv("EXIT_TIME_BUFFER_SECONDS", "30"))

# --- Risk Management ---
DAILY_LOSS_LIMIT_PCT: float = float(os.getenv("DAILY_LOSS_LIMIT_PCT", "0.20"))
DRAWDOWN_KILL_SWITCH_PCT: float = float(os.getenv("DRAWDOWN_KILL_SWITCH_PCT", "0.40"))
MAX_CONCURRENT_POSITIONS: int = int(os.getenv("MAX_CONCURRENT_POSITIONS", "5"))
MAX_CATEGORY_EXPOSURE_PCT: float = float(os.getenv("MAX_CATEGORY_EXPOSURE_PCT", "0.50"))
CONSECUTIVE_LOSSES_KILL: int = int(os.getenv("CONSECUTIVE_LOSSES_KILL", "8"))
WALLET_COOLDOWN_TRADES: int = int(os.getenv("WALLET_COOLDOWN_TRADES", "20"))
WALLET_PAUSE_MIN_TRADES: int = int(os.getenv("WALLET_PAUSE_MIN_TRADES", "10"))
WALLET_PAUSE_WR_THRESHOLD: float = float(os.getenv("WALLET_PAUSE_WR_THRESHOLD", "0.50"))
WALLET_PAUSE_CONSEC_LOSSES: int = int(os.getenv("WALLET_PAUSE_CONSEC_LOSSES", "4"))
CONVERGENCE_WINDOW_SECONDS: int = int(os.getenv("CONVERGENCE_WINDOW_SECONDS", "3600"))

# --- Paper Mode ---
PAPER_INITIAL_BALANCE_USDC: float = float(os.getenv("PAPER_INITIAL_BALANCE_USDC", "10000.0"))

# --- Telegram ---
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

# --- Logging ---
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE: str = os.getenv("LOG_FILE", "bot.log")

# --- Polling ---
POSITION_CHECK_INTERVAL_SECONDS: int = int(os.getenv("POSITION_CHECK_INTERVAL_SECONDS", "5"))
EXIT_CHECK_INTERVAL_SECONDS: int = int(os.getenv("EXIT_CHECK_INTERVAL_SECONDS", "5"))

# --- Polymarket (kept for compatibility, not used from NJ) ---
POLYMARKET_CLOB_URL: str = os.getenv("POLYMARKET_CLOB_URL", "https://clob.polymarket.com")
POLYMARKET_GAMMA_URL: str = os.getenv("POLYMARKET_GAMMA_URL", "https://gamma-api.polymarket.com")
POLY_API_KEY: str = os.getenv("POLY_API_KEY", "")
POLY_PRIVATE_KEY: str = os.getenv("POLY_PRIVATE_KEY", "")
