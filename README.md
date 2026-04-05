# Kalshi Longshot Bias Trading Bot

A Python bot that exploits the well-documented longshot bias in prediction markets on Kalshi.

## Strategy

**Longshot bias**: Markets systematically overestimate the probability of unlikely events. A YES contract priced at 10c implies a 10% chance, but research shows the true probability is closer to 6%. The bot sells against these overpriced longshots.

**Favorite bias**: The flip side — high-probability outcomes are slightly underpriced. A YES at 75c implies 75%, but the true probability is closer to 77-78%.

### How it works:

1. **Scans** Kalshi every 5 minutes for open markets matching criteria
2. **Identifies longshots**: YES contracts under 15c in sports/entertainment → buys NO
3. **Identifies favorites**: YES contracts over 70c in economics/politics → buys YES
4. **Evaluates** each opportunity with a confidence score based on edge, volume, and timing
5. **Sizes** positions as a percentage of portfolio, scaled by edge strength
6. **Manages risk** with stop-losses, drawdown limits, and position caps

## Architecture

| Module | Purpose |
|---|---|
| `config.py` | All parameters, loaded from environment |
| `kalshi.py` | Kalshi REST API wrapper (pykalshi) |
| `market_scanner.py` | Scans for longshot and favorite opportunities |
| `signal_evaluator.py` | Scores opportunities based on estimated edge |
| `position_sizer.py` | Scales positions to portfolio size |
| `risk_manager.py` | Drawdown limits, kill switches, exposure caps |
| `notifier.py` | Telegram alerts for trades and risk events |
| `bot.py` | Main async orchestration loop |

## Setup

```bash
# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with your Kalshi API credentials (see instructions in .env.example)

# Test the connection
python test_kalshi_connection.py

# Run in paper mode (default — safe, no real money)
python bot.py
```

## Getting Kalshi API Keys

1. Create an account at [kalshi.com](https://kalshi.com) (or [demo.kalshi.com](https://demo.kalshi.com) for testing)
2. Go to Settings → API Keys
3. Create a new API key — copy the **Key ID**
4. Download the **private key** (.pem file) and save it in this directory
5. Set `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH` in your `.env` file
6. Keep `KALSHI_USE_DEMO=true` until you're ready for real trading

## Configuration

Key parameters in `.env`:

| Setting | Default | Description |
|---|---|---|
| `PAPER_MODE` | `true` | Simulate trades without real money |
| `KALSHI_USE_DEMO` | `true` | Use Kalshi demo environment |
| `SCAN_INTERVAL_SECONDS` | `300` | How often to scan (5 minutes) |
| `LONGSHOT_MAX_PRICE` | `0.15` | Max YES price for longshot detection |
| `FAVORITE_MIN_PRICE` | `0.70` | Min YES price for favorite detection |
| `SIGNAL_THRESHOLD` | `0.55` | Min confidence score to trade |
| `MIN_MARKET_VOLUME` | `2000` | Min volume in USD |
| `MAX_CONCURRENT_POSITIONS` | `8` | Max open positions |
| `STOP_LOSS_PCT` | `0.50` | Stop loss threshold |
| `DAILY_LOSS_LIMIT_PCT` | `0.15` | Daily loss halt trigger |
| `DRAWDOWN_KILL_SWITCH_PCT` | `0.35` | Max drawdown before full halt |

## Risk Management

- **Daily loss limit**: Halts trading at -15% daily loss
- **Drawdown kill switch**: Full halt at -35% from peak
- **Max concurrent positions**: 8
- **Category exposure cap**: 30% of portfolio per category
- **Consecutive losses kill**: 8 losses in a row triggers halt
- **Stop loss**: Exits position at 50% loss from entry
- **Limit orders only**: Never places market orders

## Paper Mode

Paper mode (default) simulates all trading:
- No real orders placed on Kalshi
- Simulated market scanning with sample opportunities
- Simulated fills with realistic slippage
- Full logging identical to live mode

## Disclaimer

This software is for educational and research purposes. Trading prediction markets involves significant risk. Use at your own risk. Past performance does not guarantee future results. The longshot bias is a well-documented phenomenon but does not guarantee profitability.
