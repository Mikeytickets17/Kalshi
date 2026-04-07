# Kalshi Multi-Strategy Trading Bot — Project Brief

## What This Is

An automated trading bot that monitors Trump's social media, breaking news, and whale activity across prediction markets to execute trades on Kalshi, Binance, and Alpaca within seconds of market-moving events.

## Goal

Be FIRST to trade on every market-moving event — before the hedge funds and whales can fully react. The bot runs 24/7, monitors 8+ Trump social media sources and 23+ news feeds simultaneously, uses AI to analyze every post/headline, and executes trades automatically across multiple venues.

## The 5 Trading Strategies

### 1. Trump Social Media Trading
- Monitors Truth Social (API + RSS + Atom), Nitter (3 mirrors), Twitter/X (API + search) — 8 sources total
- Detects new Trump posts within 3-5 seconds of posting
- AI analyzes sentiment: BULLISH (rate cuts, crypto, peace) vs BEARISH (tariffs, war, sanctions)
- Executes BTC trades on Binance + matching Kalshi prediction contracts
- Example: Trump posts "Strategic Bitcoin Reserve signed!" → BUY BTC + YES on crypto contracts

### 2. Latency Arbitrage
- Streams real-time BTC/ETH prices from Binance/Coinbase WebSocket
- Compares CEX spot price to Kalshi crypto contract pricing
- When Kalshi contracts lag behind the real price (2-8 second delay), buys the mispriced side
- High win rate (85-95%) due to mathematical edge

### 3. Breaking News Trading
- 23+ RSS feeds: Reuters, AP, CNBC, WSJ, Federal Reserve, Treasury, BLS, CoinDesk, Politico, Google News, Reddit
- Brave Search API scans for real-time breaking news every 30 seconds
- AI classifies every headline and generates multi-venue trade actions
- Covers: Fed rate decisions, CPI/jobs data, tariffs, geopolitical events, earnings
- Example: "Fed cuts rates 50bp" → BUY BTC + BUY SPY + LONG BTC futures + YES on Fed contract

### 4. Kalshi Contract Matching
- When any signal fires (Trump post, news headline, whale activity), searches ALL open Kalshi markets
- Matches keywords to contract titles (e.g., "tariff" + "china" → "Will US impose >50% tariffs on China?")
- Buys the correct side (YES/NO) based on the event
- Fetches up to 500 open markets from Kalshi API

### 5. Whale Copy Trading
- Scans ALL Kalshi markets every 15 seconds
- Detects volume spikes (3x+ normal), price jumps (8%+), order book imbalance (3:1)
- When whale activity is detected, copies the trade direction
- Dashboard shows whale signals in real-time

## Architecture

```
DATA SOURCES (real-time)          ANALYSIS              EXECUTION           MONITORING
─────────────────────            ────────              ─────────           ──────────
Truth Social API ─────┐                                                   
Truth Social RSS ─────┤                                                   
Truth Social Atom ────┤          AI Provider           Kalshi API ──┐     Dashboard
Nitter (3 mirrors) ───┤──→ (Claude/Groq/Gemini/ ──→   Binance ─────┼──→  (localhost:5050)
Twitter/X API ────────┤    Ollama/OpenRouter/Rules)    Alpaca ──────┘     
Twitter/X Search ─────┘                                                   Telegram Alerts
                                                                          
Reuters RSS ──────────┐                                                   Research Scanner
AP RSS ───────────────┤          News Analyzer                            (Brave Search)
CNBC RSS ─────────────┤──→ (same AI providers) ──→    Same venues         
Fed/Treasury/BLS ─────┤                                                   
Google News ──────────┤                                                   
Reddit ───────────────┤                                                   
Brave Search ─────────┘                                                   
                                                                          
Binance WebSocket ────┤                                                   
Coinbase WebSocket ───┤──→ Edge Detection ──→          Kalshi              
                                                                          
Kalshi Market Data ───┤──→ Whale Detection ──→         Kalshi (copy)      
```

## Tech Stack

- **Language:** Python 3.12 (async/await throughout)
- **Dashboard:** Flask + vanilla HTML/JS (no React — fast, simple)
- **AI:** Multi-provider (Anthropic Claude, Groq, Google Gemini, Ollama, OpenRouter)
- **Data:** httpx for HTTP, websockets for real-time streams
- **Persistence:** JSON file (shared_state.py) + localStorage in browser
- **Deployment:** Docker + docker-compose for 24/7 VPS operation
- **Notifications:** Telegram Bot API with 8 alert types

## Key Files

| File | Purpose |
|------|---------|
| `bot.py` | Main orchestration — runs all 5 strategies concurrently |
| `trump_monitor.py` | 8-source Trump post detection (3-second latency target) |
| `sentiment_analyzer.py` | AI-powered post analysis (BULLISH/BEARISH/NEUTRAL) |
| `news_feed.py` | 23+ RSS feeds + Brave Search + Reddit + Google News |
| `news_analyzer.py` | Headline-to-trade-action conversion |
| `whale_tracker.py` | Kalshi market flow detection + copy trading |
| `kalshi_client.py` | Kalshi REST API (orders, markets, positions) |
| `exchange.py` | Binance spot BTC/ETH trading |
| `stock_trader.py` | Alpaca US stock trading |
| `contract_matcher.py` | Maps events to Kalshi prediction contracts |
| `ai_provider.py` | Multi-provider AI routing (5 providers + fallback) |
| `risk_manager.py` | Drawdown limits, stop losses, position caps |
| `shared_state.py` | Bot-to-dashboard state bridge (JSON persistence) |
| `dashboard.py` | Flask web server for the dashboard |
| `dashboard.html` | Real-time dashboard UI |
| `notifier.py` | Telegram alerts (8 types: trades, signals, daily summary) |
| `research_scanner.py` | Overnight Brave Search research + morning reports |
| `backtest.py` | Strategy backtesting with Sharpe ratio, profit factor |
| `multi_account.py` | Distribute trades across multiple Kalshi accounts |
| `config.py` | All configuration via environment variables |

## Risk Management

- **Daily loss limit:** 20% of portfolio → halts trading
- **Max drawdown kill switch:** 40% → shuts down everything
- **Consecutive loss kill:** 8 losses in a row → pause
- **Max concurrent positions:** 5 (configurable)
- **Per-trade stop loss:** 50% of position
- **Category exposure cap:** 50% in any single category
- **Wallet-level performance tracking:** pause underperforming strategies

## What's Needed to Run

**Minimum (free, paper mode):**
```
PAPER_MODE=true
GROQ_API_KEY=<free at console.groq.com>
BRAVE_API_KEY=<your key>
```

**Full (real trading):**
```
PAPER_MODE=false
KALSHI_API_KEY_ID=<from kalshi.com>
KALSHI_PRIVATE_KEY_PATH=<path to .pem>
ANTHROPIC_API_KEY=<or GROQ_API_KEY for free>
BRAVE_API_KEY=<your key>
BINANCE_API_KEY=<optional, for BTC spot>
TELEGRAM_BOT_TOKEN=<optional, for alerts>
```

## Current Status

- All code written and tested (47+ automated tests)
- Paper mode fully functional
- Dashboard live at GitHub Pages
- Ready for real API keys and overnight testing
- Research scanner ready for continuous strategy discovery

## Next Steps / Ideas to Explore

1. **Kalshi WebSocket** — If Kalshi adds WebSocket support, switch from polling to streaming for faster whale detection
2. **Sentiment momentum** — Track sentiment over time, not just individual posts. Rising bullish sentiment over 1 hour = stronger signal
3. **Cross-market arbitrage** — Compare Kalshi contract prices to Polymarket/PredictIt for the same events
4. **Options-like strategies** — Buy both YES and NO on high-volatility contracts before major events, sell the winning side
5. **Machine learning** — Train a model on historical Kalshi data to predict which contracts will move
6. **Order flow toxicity** — Measure whether the volume is informed (directional) vs noise
7. **Event calendar integration** — Pre-position before known events (FOMC, CPI release dates, Trump rallies)
