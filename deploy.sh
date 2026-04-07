#!/bin/bash
# Deploy Kalshi bot to a VPS
# Usage: ./deploy.sh [start|stop|restart|logs|status]

set -e
cd "$(dirname "$0")"

case "${1:-start}" in
  start)
    echo "Starting Kalshi bot..."
    docker compose up -d --build
    echo "Dashboard: http://$(hostname -I | awk '{print $1}'):5050"
    ;;
  stop)
    echo "Stopping..."
    docker compose down
    ;;
  restart)
    echo "Restarting..."
    docker compose restart
    ;;
  logs)
    docker compose logs -f --tail=100
    ;;
  status)
    docker compose ps
    echo ""
    echo "Bot state:"
    python3 -c "import json; s=json.load(open('bot_state.json')); print(f'Portfolio: \${s[\"portfolio_value\"]:,.2f}  Trades: {s[\"trade_count\"]}  Running: {s[\"bot_running\"]}')" 2>/dev/null || echo "No state file yet"
    ;;
  *)
    echo "Usage: ./deploy.sh [start|stop|restart|logs|status]"
    ;;
esac
