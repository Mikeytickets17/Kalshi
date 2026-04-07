#!/bin/bash
# ═══════════════════════════════════════════════════
# Kalshi Multi-Strategy Trading Bot — Launcher
# ═══════════════════════════════════════════════════
#
# Starts both the trading bot and the dashboard.
# Dashboard: http://localhost:5050
#
# Usage:
#   ./start.sh          # Start everything (paper mode by default)
#   ./start.sh --bot    # Start bot only
#   ./start.sh --dash   # Start dashboard only
#
# ═══════════════════════════════════════════════════

set -e
cd "$(dirname "$0")"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

echo -e "${CYAN}═══════════════════════════════════════════════════${NC}"
echo -e "${CYAN}  Kalshi Multi-Strategy Trading Bot${NC}"
echo -e "${CYAN}═══════════════════════════════════════════════════${NC}"
echo ""

# Check Python
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}[ERROR] python3 not found. Install Python 3.10+${NC}"
    exit 1
fi

# Check .env
if [ ! -f .env ]; then
    if [ -f .env.example ]; then
        echo -e "${YELLOW}[SETUP] No .env file found. Creating from .env.example...${NC}"
        cp .env.example .env
        echo -e "${YELLOW}[SETUP] Edit .env to add your API keys (paper mode works without them)${NC}"
    else
        echo -e "${YELLOW}[WARN] No .env file — using defaults (paper mode)${NC}"
    fi
fi

# Check dependencies
echo -e "${CYAN}[CHECK] Verifying dependencies...${NC}"
python3 -c "import httpx, flask, dotenv" 2>/dev/null || {
    echo -e "${YELLOW}[SETUP] Installing dependencies...${NC}"
    pip install -r requirements.txt --quiet 2>/dev/null || pip install -r requirements.txt --quiet --break-system-packages 2>/dev/null
}

# Parse args
MODE="${1:-all}"

cleanup() {
    echo ""
    echo -e "${YELLOW}[SHUTDOWN] Stopping all processes...${NC}"
    kill $(jobs -p) 2>/dev/null
    wait 2>/dev/null
    echo -e "${GREEN}[DONE] All processes stopped.${NC}"
}
trap cleanup EXIT INT TERM

if [ "$MODE" = "--bot" ]; then
    echo -e "${GREEN}[START] Bot only${NC}"
    python3 bot.py
elif [ "$MODE" = "--dash" ]; then
    echo -e "${GREEN}[START] Dashboard only → http://localhost:5050${NC}"
    python3 dashboard.py
else
    # Start both
    echo -e "${GREEN}[START] Bot (background)${NC}"
    python3 bot.py &
    BOT_PID=$!
    sleep 1

    echo -e "${GREEN}[START] Dashboard → http://localhost:5050${NC}"
    echo ""
    echo -e "${CYAN}  Open your browser to: ${GREEN}http://localhost:5050${NC}"
    echo -e "${CYAN}  Press Ctrl+C to stop everything${NC}"
    echo ""
    python3 dashboard.py &
    DASH_PID=$!

    # Wait for either to exit
    wait -n $BOT_PID $DASH_PID 2>/dev/null
fi
