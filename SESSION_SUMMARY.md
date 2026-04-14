# KALSHI TRADING BOT — FULL SESSION SUMMARY (Updated April 13, 2026)
# Repo: github.com/Mikeytickets17/Kalshi
# Branch: claude/setup-kalshi-bot-vh0aA
# Dashboard: https://mikeytickets17.github.io/Kalshi/
# User's machine: Windows, VS Code, C:\Users\mikey\Kalshi

## HOW TO START A NEW CHAT
Paste this to the new Claude:
"Read SESSION_SUMMARY.md in the Kalshi repo on branch claude/setup-kalshi-bot-vh0aA.
That has full context. The repo is Mikeytickets17/Kalshi on GitHub."

## CRITICAL RULES (FROM THE USER)
1. NEVER simulate or fake ANY data. Real data only. No Math.random() trades.
2. BTC and ETH ONLY. No stock trades (SPY, QQQ, USO, LMT). Ever.
3. No contradictory trades — don't BUY and SELL BTC at the same time.
4. Pick ONE direction based on all available analysis (news, order flow, whales).
5. Check code 5x before delivering. User does NOT want to debug.
6. Don't give code for PowerShell — user runs everything in VS Code terminal.
7. Don't use localhost — dashboard is at mikeytickets17.github.io/Kalshi/
8. The bot runs 24/7 on user's Windows machine (never turned off).
9. No endless terminal spam — silence noisy HTTP logs.
10. Everything must be real. If it's not real, don't show it.

## CURRENT STATE (April 13, 2026)

### What's Running
- Bot: `python watchdog.py` in VS Code terminal
- Watchdog auto-restarts bot on crash, pulls latest code
- Mode: LIVE (PAPER_MODE=false)
- Kalshi: AUTHENTICATED on demo (KALSHI_USE_DEMO=true)
- Dashboard: CLEAN fresh start, $10,000, zero trades
- Publisher: pushes bot_state.json to gh-pages every 30 seconds via GitHub API

### What's Connected (REAL)
- Kalshi demo API: authenticated, fetching real contracts
- Coinbase WebSocket: connected, real BTC/ETH prices
- Coinbase REST API: real spot prices for spread reader
- CoinGecko/CryptoCompare: fallback price feeds
- 24+ RSS feeds: CNBC, Reuters, Fed, Politico, MarketWatch, CoinTelegraph, etc.
- Brave Search API: real-time news scanning
- Polymarket Gamma API: public, no auth needed, BTC contract prices
- Truth Social: polling for Trump posts
- GitHub Pages publisher: pushes state every 30s

### What's NOT Connected
- Binance WebSocket: blocked in US (HTTP 451) — uses Coinbase instead
- Binance spot trading: no API key (not needed, Kalshi is the venue)
- Alpaca stocks: DISABLED — BTC/ETH only
- Telegram alerts: not configured (user can add TELEGRAM_BOT_TOKEN)
- Apprise alerts: installed, needs APPRISE_URLS in .env

## .env KEYS CONFIGURED (on user's Windows machine)
- KALSHI_API_KEY_ID=✓ (set)
- KALSHI_PRIVATE_KEY_PATH=./mikebot.pem ✓
- KALSHI_USE_DEMO=true
- PAPER_MODE=false
- BRAVE_API_KEY=✓ (set)
- GROQ_API_KEY=✓ (set)
- GITHUB_TOKEN=✓ (set, for dashboard publishing)
- GITHUB_REPO=Mikeytickets17/Kalshi
- BINANCE_API_KEY= (empty, not needed)
- ALPACA_API_KEY= (empty, DISABLED)
- TELEGRAM_BOT_TOKEN= (empty)
- APPRISE_URLS= (empty)

## ALL FILES IN THE BOT

### Core Bot
- bot.py — Main orchestration, 8 strategies running concurrently
- config.py — All configuration via environment variables
- shared_state.py — Bot-to-dashboard state bridge (JSON persistence)
- watchdog.py — 24/7 auto-restart launcher (Huginn-style)

### Trading Strategies
- market_scanner.py — Latency arb: CEX price vs Kalshi crypto contracts
- edge_scanner.py — 3 edge strategies: cross-venue arb, settlement sniper, bracket arb
- spread_reader.py — Polymarket vs Coinbase spread detection
- flow_analyzer.py — CVD, VWAP, absorption, sweep detection, realized vol
- trump_monitor.py — 8-source Trump post detection (Truth Social, Twitter, Nitter, RSS)
- news_feed.py — 24+ RSS feeds + Brave Search
- news_analyzer.py — AI headline-to-trade-action conversion
- whale_tracker.py — Kalshi volume spike + order flow detection
- contract_matcher.py — Maps events to Kalshi prediction contracts
- sentiment_analyzer.py — AI-powered post analysis

### Exchange Clients
- kalshi_client.py — Kalshi REST API (pykalshi v1.0)
  - Uses: KalshiClient(api_key_id=, private_key_path=, demo=)
  - Methods: get_markets(), get_market(ticker), create_order()
  - Market objects have: ticker, title, yes_ask_dollars, last_price_dollars,
    close_time, status, result, volume_fp
- exchange.py — Binance spot BTC/ETH trading
- stock_trader.py — Alpaca (DISABLED)
- polymarket.py — Polymarket CLOB API

### Analysis
- orderbook.py — 4-level depth analysis, weighted book pressure
- position_sizer.py — Kelly Criterion with per-signal tracking
- signal_evaluator.py — 5-factor confidence scoring
- risk_manager.py — Drawdown limits, stop losses, kill switches
- ai_provider.py — Multi-provider AI routing (Claude, Groq, Gemini, Ollama)

### Dashboard & Alerts
- dashboard.html — Real-time dashboard (also pushed to gh-pages as index.html)
- dashboard.py — Flask local dashboard server
- ghpages_publisher.py — Pushes bot_state.json to gh-pages via GitHub API
- alerts.py — Apprise multi-channel alerts (90+ services)
- notifier.py — Telegram alerts

### Other
- backtest.py — Strategy backtesting
- research_scanner.py — Overnight Brave Search research
- live_scanner.py — Terminal-based live display
- multi_account.py — Multi-account Kalshi manager
- edge_scanner.py — Settlement sniper, cross-venue arb, bracket arb
- setup_vps.sh — One-click VPS deployment
- SESSION_SUMMARY.md — This file

## KEY BUGS FIXED THIS SESSION
1. pykalshi import: changed `from pykalshi import HttpClient` to `from pykalshi import KalshiClient`
2. pykalshi API: `client.get_markets()` not `client.markets.get()`
3. Market objects: use `getattr(m, 'yes_ask_dollars')` not `m.get("yes_ask")`
4. Kalshi private key path: fixed from `./mikebot./mikebot` to `./mikebot.pem`
5. Windows signal handler: wrapped in try/except for NotImplementedError
6. Unicode logging: `→` character crashes Windows cp1252 encoding
7. PAPER_MODE: was true, now false for real trading
8. Dashboard pollBotState: was resetting to STANDALONE every 3s on GitHub Pages
9. Dashboard loadBotState: wasn't updating BOT CONNECTED badge
10. P&L calculation: initial_balance was set to recovered value, not $10,000
11. Unrealized P&L: was always $0 — now calculates from real spot vs strike
12. Stock trades: were executing SPY/USO/LMT — now blocked, BTC/ETH only
13. Contradictory trades: was buying AND selling BTC — now blocked
14. GitHub token: wasn't in .env — had to write it via Python
15. localStorage cache: old SPY trades stuck in browser — cleared with localStorage.clear()

## HOW TO RESTART THE BOT
In VS Code terminal:
```
cd C:\Users\mikey\Kalshi
git pull origin claude/setup-kalshi-bot-vh0aA
python watchdog.py
```

## HOW TO GO LIVE WITH REAL MONEY
Change ONE line in .env:
```
KALSHI_USE_DEMO=false
```
Then restart the bot.

## NEXT STEPS / USER WANTS
1. Government data release sniper (CPI, jobs at 8:30 AM ET) — not built yet
2. Kalshi WebSocket streaming for real-time order book — not built yet
3. Better AI analysis (GPT researcher for patterns and events)
4. n8n automation integration
5. OpenClaw integration for terminal monitoring (user has it installed)
6. Huginn-style automation (watchdog.py is the basic version)
7. Apprise alerts configured and working
8. Real unrealized P&L that updates live on dashboard
9. Settlement sniper finding actual near-expiry opportunities
10. Cross-venue arb (Kalshi vs Polymarket) executing real trades

## ARCHITECTURE
```
User's Windows Machine (always on)
├── VS Code Terminal: python watchdog.py
│   └── watchdog.py auto-restarts bot.py on crash
│       ├── Kalshi Demo API (authenticated)
│       ├── Coinbase WebSocket (real BTC/ETH prices)
│       ├── Polymarket Gamma API (public, spread detection)
│       ├── 24+ RSS feeds (real news)
│       ├── Brave Search API (breaking news)
│       ├── Truth Social (Trump posts)
│       ├── Flow Analyzer (CVD, VWAP, absorption)
│       ├── Edge Scanner (settlement snipe, arb)
│       ├── Spread Reader (Polymarket vs Coinbase)
│       └── GHPages Publisher → pushes bot_state.json every 30s
│
├── GitHub Pages: mikeytickets17.github.io/Kalshi/
│   ├── index.html (dashboard, zero simulation)
│   └── bot_state.json (pushed by bot every 30s)
│
└── OpenClaw (installed, can monitor terminal)
```
