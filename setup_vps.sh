#!/bin/bash
# ============================================================
#  KALSHI TRADING BOT — ONE-CLICK VPS SETUP
#
#  Paste this ENTIRE script into your VPS terminal.
#  It does everything: installs Docker, clones repo, starts bot.
#
#  Before running, you need:
#    1. Your Kalshi API Key ID
#    2. Your Kalshi private key (.pem file content)
#    3. Your Brave API key
#    4. Your Groq/Anthropic/Gemini API key
# ============================================================

set -e
echo "========================================"
echo "  KALSHI BOT — AUTOMATED VPS SETUP"
echo "========================================"

# --- Install Docker ---
echo "[1/6] Installing Docker..."
if ! command -v docker &> /dev/null; then
    curl -fsSL https://get.docker.com | sh
    systemctl enable docker
    systemctl start docker
    echo "  Docker installed."
else
    echo "  Docker already installed."
fi

# --- Install Docker Compose ---
echo "[2/6] Checking Docker Compose..."
if ! docker compose version &> /dev/null; then
    apt-get update -qq && apt-get install -y -qq docker-compose-plugin
fi
echo "  Docker Compose ready."

# --- Clone the repo ---
echo "[3/6] Cloning Kalshi bot..."
cd /root
if [ -d "Kalshi" ]; then
    cd Kalshi
    git fetch origin
    git checkout claude/setup-kalshi-bot-vh0aA
    git pull origin claude/setup-kalshi-bot-vh0aA
else
    git clone https://github.com/Mikeytickets17/Kalshi.git
    cd Kalshi
    git checkout claude/setup-kalshi-bot-vh0aA
fi
echo "  Repo ready."

# --- Collect API keys ---
echo ""
echo "========================================"
echo "  ENTER YOUR API KEYS"
echo "========================================"
echo ""

read -p "Kalshi API Key ID: " KALSHI_KEY_ID
echo ""
echo "Paste your Kalshi private key (.pem content)."
echo "When done, press Enter then Ctrl+D:"
KALSHI_PEM=$(cat)
echo ""
read -p "Brave Search API Key: " BRAVE_KEY
echo ""
read -p "AI Provider Key (Groq/Anthropic/Gemini): " AI_KEY
echo ""
read -p "Which AI provider? (groq/anthropic/gemini) [groq]: " AI_PROVIDER
AI_PROVIDER=${AI_PROVIDER:-groq}
echo ""
read -p "Start with DEMO mode? (yes/no) [yes]: " USE_DEMO
USE_DEMO=${USE_DEMO:-yes}

DEMO_FLAG="true"
if [ "$USE_DEMO" = "no" ]; then
    DEMO_FLAG="false"
fi

# --- Save private key ---
echo "[4/6] Saving credentials..."
echo "$KALSHI_PEM" > /root/Kalshi/kalshi_private_key.pem
chmod 600 /root/Kalshi/kalshi_private_key.pem

# --- Create .env ---
AI_LINE=""
case "$AI_PROVIDER" in
    anthropic) AI_LINE="ANTHROPIC_API_KEY=$AI_KEY" ;;
    gemini)    AI_LINE="GEMINI_API_KEY=$AI_KEY" ;;
    *)         AI_LINE="GROQ_API_KEY=$AI_KEY" ;;
esac

cat > /root/Kalshi/.env << ENVEOF
PAPER_MODE=false
KALSHI_API_KEY_ID=$KALSHI_KEY_ID
KALSHI_PRIVATE_KEY_PATH=./kalshi_private_key.pem
KALSHI_USE_DEMO=$DEMO_FLAG

$AI_LINE

BRAVE_API_KEY=$BRAVE_KEY

BINANCE_WS_URL=wss://stream.binance.com:9443/ws
COINBASE_WS_URL=wss://ws-feed.exchange.coinbase.com

POLYMARKET_CLOB_URL=https://clob.polymarket.com
POLYMARKET_GAMMA_URL=https://gamma-api.polymarket.com

EDGE_THRESHOLD_PCT=0.03
MAX_EDGE_PCT=0.15
TARGET_ASSETS=BTC,ETH
TARGET_DURATIONS=15,60

TRUMP_POLL_INTERVAL_SECONDS=3.0
TRUMP_MIN_CONFIDENCE=0.45
TRUMP_TRADE_SIZE_PCT=0.06
TRUMP_MAX_TRADE_SIZE_USDC=750.0
TRUMP_HOLD_MINUTES=20

BASE_COPY_PCT=0.05
MAX_SINGLE_POSITION_PCT=0.08
MIN_TRADE_SIZE_USDC=1.0
MAX_TRADE_SIZE_USDC=500.0

DAILY_LOSS_LIMIT_PCT=0.20
DRAWDOWN_KILL_SWITCH_PCT=0.40
MAX_CONCURRENT_POSITIONS=5
CONSECUTIVE_LOSSES_KILL=8
STOP_LOSS_PCT=0.50

PAPER_INITIAL_BALANCE_USDC=10000.0
LOG_LEVEL=INFO
LOG_FILE=bot.log
ENVEOF

echo "  Credentials saved."

# --- Open firewall for dashboard ---
echo "[5/6] Opening firewall for dashboard..."
ufw allow 5050/tcp 2>/dev/null || true
echo "  Port 5050 open."

# --- Deploy ---
echo "[6/6] Starting bot + dashboard..."
cd /root/Kalshi
docker compose up -d --build

echo ""
echo "========================================"
echo "  DEPLOYMENT COMPLETE"
echo "========================================"
VPS_IP=$(hostname -I | awk '{print $1}')
echo ""
echo "  Dashboard: http://$VPS_IP:5050"
echo ""
echo "  Commands:"
echo "    ./deploy.sh logs     — watch live trading"
echo "    ./deploy.sh status   — check portfolio"
echo "    ./deploy.sh restart  — restart bot"
echo "    ./deploy.sh stop     — stop everything"
echo ""
if [ "$DEMO_FLAG" = "true" ]; then
    echo "  MODE: DEMO (fake money on Kalshi, real data)"
    echo "  To go LIVE with real money:"
    echo "    1. Edit .env: change KALSHI_USE_DEMO=false"
    echo "    2. Run: ./deploy.sh restart"
else
    echo "  MODE: LIVE (REAL MONEY)"
    echo "  Bot is now trading with real capital!"
fi
echo ""
echo "========================================"
