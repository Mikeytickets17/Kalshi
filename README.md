# Polymarket Copy-Trading Bot

A Python bot that mirrors positions from high-performing Polymarket wallets.

## Strategy

1. Maintains a watchlist of verified high-win-rate Polymarket wallets
2. Monitors those wallets for new position entries via Polygon RPC
3. Evaluates each detected trade with filters and confidence scoring
4. Executes a scaled copy of qualifying positions via the Polymarket CLOB API
5. Manages exits independently using configurable risk rules

## Architecture

| Module | Purpose |
|---|---|
| `config.py` | All parameters, loaded from environment |
| `polymarket.py` | Polymarket CLOB/Gamma API wrapper |
| `wallet_tracker.py` | Monitors wallets for new Polymarket trades on Polygon |
| `signal_evaluator.py` | Filters and scores trade signals before copying |
| `wallet_ranker.py` | Periodically re-scores wallets from leaderboard/Dune data |
| `position_sizer.py` | Scales copied positions to portfolio size |
| `risk_manager.py` | Drawdown limits, kill switches, exposure caps |
| `notifier.py` | Telegram alerts for trades, risks, and summaries |
| `bot.py` | Main orchestration loop |

## Setup

```bash
# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with your API keys

# Edit wallets.json with wallets to track

# Run in paper mode (default)
python bot.py
```

## Configuration

All settings are configurable via environment variables or `.env` file. Key parameters:

- `PAPER_MODE` — `true` (default) for simulated trading
- `COPY_THRESHOLD` — Minimum confidence score to copy a trade (default: 0.60)
- `BASE_COPY_PCT` — Base position size as % of portfolio (default: 3%)
- `MAX_CONCURRENT_POSITIONS` — Maximum open positions (default: 8)
- `STOP_LOSS_PCT` — Stop loss threshold (default: 50%)
- `DAILY_LOSS_LIMIT_PCT` — Daily loss halt trigger (default: 15%)
- `DRAWDOWN_KILL_SWITCH_PCT` — Max drawdown before full halt (default: 35%)

See `.env.example` for the complete list.

## Risk Management

- **Daily loss limit**: Trading halts at -15% daily loss
- **Drawdown kill switch**: Full halt at -35% from peak
- **Max concurrent positions**: 8
- **Category exposure cap**: 30% of portfolio per category
- **Consecutive losses kill**: 8 losses in a row triggers halt
- **Stop loss**: Exits position at 50% loss from entry

## Paper Mode

Paper mode simulates all trading activity:
- No real orders placed
- Simulated wallet trade detections with 3-8 second delay
- Simulated price fills with slippage
- Full logging identical to live mode

## Wallet Ranking

The bot automatically re-ranks wallets every 6 hours using:
- Polymarket public leaderboard
- Dune Analytics queries
- Scoring formula weighting win rate (40%), trade volume (25%), Sharpe estimate (20%), and recency (15%)

Wallets below the minimum score threshold are automatically deactivated.

## Disclaimer

This software is for educational and research purposes. Trading prediction markets involves significant risk. Use at your own risk.
