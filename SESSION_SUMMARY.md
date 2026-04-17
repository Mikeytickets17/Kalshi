# KALSHI TRADING BOT — FULL SESSION SUMMARY (Updated through ~April 14, 2026)
# Repo: github.com/Mikeytickets17/Kalshi
# Branch: claude/setup-kalshi-bot-vh0aA
# Dashboard: https://mikeytickets17.github.io/Kalshi/
# User's machine: Windows, VS Code, C:\Users\mikey\Kalshi

## HOW TO START A NEW CHAT
Paste this to the new Claude:
"Read SESSION_SUMMARY.md in the Kalshi repo on branch claude/setup-kalshi-bot-vh0aA.
That has full context. The repo is Mikeytickets17/Kalshi on GitHub."

## USER'S STRICT RULES (VIOLATING THESE = USER GETS ANGRY)
1. NEVER simulate or fake ANY data. Real data only. No Math.random() trades. EVER.
2. BTC and ETH ONLY. No stock trades (SPY, QQQ, USO, LMT). No exceptions.
3. No contradictory trades — don't BUY and SELL BTC at the same time.
4. Pick ONE direction based on all analysis (news, order flow, whales).
5. Check code 5x before delivering. User does NOT want to debug your mistakes.
6. Don't give code for PowerShell — user runs everything in VS Code terminal.
7. Don't use localhost — dashboard is at mikeytickets17.github.io/Kalshi/
8. Bot runs 24/7 on user's Windows machine (never turned off).
9. No endless terminal spam — silence noisy HTTP logs.
10. If not real, don't show it.
11. User is impatient and wants one-shot fixes. Trace every bug fix end-to-end.
12. Don't keep sending commands blindly — verify and think first.

## CURRENT STATE (last known)

### What's Running
- Bot: `python watchdog.py` in VS Code terminal
- Watchdog auto-restarts bot on crash, pulls latest code
- Mode: LIVE (PAPER_MODE=false, KALSHI_USE_DEMO=false)
- Kalshi: AUTHENTICATED on PRODUCTION API (switched from demo)
- Safety: MAX_TRADE_SIZE=$0, MIN_TRADE_SIZE=$99999 so no orders execute
- Dashboard: CLEAN fresh start, $10,000 baseline, zero trades
- Publisher: pushes bot_state.json to gh-pages every 30 seconds via GitHub API

### What's Connected (REAL)
- Kalshi LIVE API: authenticated, fetching real contracts (but 0 crypto found)
- Coinbase WebSocket: connected, real BTC/ETH prices
- Coinbase REST API: real spot prices
- CoinGecko/CryptoCompare: fallback price feeds
- 24+ RSS feeds: CNBC, Reuters, Fed, Politico, MarketWatch, CoinTelegraph, Hill
- Brave Search API: real-time news scanning
- Polymarket Gamma API: public, no auth, BTC contract prices (READ-ONLY, blocked in US)
- Truth Social: polling for Trump posts (paper simulated posts when not found)
- GitHub Pages publisher: pushes state every 30s via GitHub API

### THE BIG BLOCKER RIGHT NOW
`get_markets(limit=1000)` on Kalshi LIVE returns 0 crypto contracts.
Only returns: KXMVECROSSCATEGORY, KXMVESPORTSMULTIGAMEEXTENDED, KXTOPSERIESAMC, KXTOPSERIESATV, KXTOPSERIESDIS, KXTOPSERIESHUL (sports/TV contracts).

Kalshi events available include: BTCETHATH (Bitcoin/ETH all-time high) but NOT the 15-min price contracts.

The KXBTCD (BTC daily) and similar 15-min crypto contracts are either:
- Hidden behind pagination (cursor-based)
- Require filtering by series_ticker parameter
- Not available on demo at all

NEXT STEP: Use `get_series_list` or `get_events(series_ticker="KXBTCD")` or pagination to find crypto.

### What's NOT Connected
- Binance WebSocket: blocked in US (HTTP 451) — uses Coinbase instead
- Binance spot trading: no API key (not needed, Kalshi is the venue)
- Alpaca stocks: DISABLED — BTC/ETH only rule
- Telegram alerts: not configured
- Apprise alerts: installed, APPRISE_URLS empty in .env

## .env KEYS CONFIGURED (on user's Windows machine)
- KALSHI_API_KEY_ID=✓ (set)
- KALSHI_PRIVATE_KEY_PATH=./mikebot.pem ✓ (file name is literally "mikebot.pem")
- KALSHI_USE_DEMO=false (switched from true — demo only had Chainlink contracts)
- PAPER_MODE=false
- BRAVE_API_KEY=✓ (set)
- GROQ_API_KEY=✓ (set)
- GITHUB_TOKEN=✓ (set, for dashboard publishing)
- GITHUB_REPO=Mikeytickets17/Kalshi
- MAX_TRADE_SIZE_USDC=0.0 (prevents all orders)
- MIN_TRADE_SIZE_USDC=99999.0 (prevents all orders)
- BINANCE_API_KEY= (empty, not needed)
- ALPACA_API_KEY= (empty, DISABLED)
- TELEGRAM_BOT_TOKEN= (empty)
- APPRISE_URLS= (empty)

## ALL FILES

### Core
- bot.py — Main orchestration, 8 strategies concurrent
- config.py — Environment variables
- shared_state.py — Bot-to-dashboard state bridge
- watchdog.py — 24/7 auto-restart launcher (Huginn-style)

### Strategies
- market_scanner.py — Latency arb
- edge_scanner.py — Cross-venue arb, settlement sniper, bracket arb
- spread_reader.py — Polymarket-Coinbase spread detection
- flow_analyzer.py — CVD, VWAP, absorption, sweep detection
- trump_monitor.py — 8-source Trump post detection
- news_feed.py — 24+ RSS feeds + Brave Search
- news_analyzer.py — AI headline-to-trade conversion
- whale_tracker.py — Kalshi volume/price spike detection
- contract_matcher.py — Event-to-contract mapping
- sentiment_analyzer.py — AI sentiment

### Exchange
- kalshi_client.py — pykalshi v1.0 API
  - KalshiClient(api_key_id=, private_key_path=, demo=)
  - Methods: get_markets(), get_market(ticker), create_order(), get_events(), get_all_series(), paginated_get
  - Market attrs: ticker, title, yes_ask_dollars, last_price_dollars, close_time, status, result, volume_fp, series_ticker, event_ticker, subtitle
- exchange.py — Binance (not used, US blocked)
- stock_trader.py — Alpaca (DISABLED)
- polymarket.py — Polymarket CLOB

### Dashboard
- dashboard.html — Real-time (also index.html on gh-pages)
- dashboard.py — Flask local server
- ghpages_publisher.py — Pushes bot_state.json to gh-pages via GitHub API
- alerts.py — Apprise multi-channel
- notifier.py — Telegram

### Other
- SESSION_SUMMARY.md — This file
- setup_vps.sh — One-click VPS deploy
- backtest.py, research_scanner.py, live_scanner.py, multi_account.py

## KEY BUGS FIXED THIS SESSION
1. pykalshi import: `HttpClient` → `KalshiClient`
2. pykalshi API: `client.get_markets()` (direct), not `client.markets.get()`
3. Market objects: `getattr(m, 'yes_ask_dollars')` not `m.get()`
4. Kalshi private key path: `./mikebot.pem` (was wrong before)
5. Windows signal handler crash: wrapped in try/except
6. Unicode logging errors: `→` crashes Windows cp1252
7. PAPER_MODE: flipped to false for real data
8. Dashboard pollBotState was resetting STANDALONE every 3s on GitHub Pages
9. loadBotState wasn't updating BOT CONNECTED badge
10. P&L: initial_balance now always $10,000, not recovered value
11. Unrealized P&L: calculates from real spot vs strike every 3s
12. Stock trades (SPY/USO/LMT): completely blocked
13. Contradictory trades: BUY+SELL same asset blocked
14. GitHub token: Python script to write to .env (PowerShell echo issues)
15. localStorage cache: browser showing old SPY trades, cleared with localStorage.clear()
16. Kalshi demo has ONLY Chainlink (KXLTCD) — switched to production
17. Production Kalshi get_markets returns sports/TV, need pagination for crypto

## HOW TO RESTART
```
cd C:\Users\mikey\Kalshi
git pull origin claude/setup-kalshi-bot-vh0aA
python watchdog.py
```

## HOW TO GO LIVE WITH REAL MONEY
1. Fix Kalshi crypto contract fetching (current blocker)
2. Fund Kalshi account at kalshi.com/wallet ($100-200 to start)
3. Edit .env:
   ```
   MAX_TRADE_SIZE_USDC=50
   MIN_TRADE_SIZE_USDC=5
   ```
4. Restart bot

## NEXT STEPS / TODO
1. **URGENT: Fix Kalshi crypto contract discovery** — use pagination or series_ticker filter to find KXBTCD / KXETH contracts
2. Government data release sniper (CPI, jobs at 8:30 AM ET)
3. Kalshi WebSocket streaming for real-time order book
4. Smart whale tracker: only copy wallets with >70% win rate over 50+ trades
5. Apprise alerts configuration
6. Better AI analysis for event pattern recognition
7. n8n automation integration
8. Polymarket whale wallet copy trading (read the tweet — $25→$12,582 was marketing but the 3 repos are real: poly_data, polyterm, py-clob)

## WHAT USER ASKED ABOUT RECENTLY
- User saw tweet from @AleiahLock claiming $25 → $12,582 overnight with Polymarket wallet copy trading using 3 GitHub repos (poly_data, polyterm, py-clob) and 6 Claude agents. Had referral link to Telegram bot (scam).
- User wants: smart whale tracking with track records, not just volume spikes
- User has OpenClaw installed (can control terminal)
- User wants n8n automation, GPT researcher integration

## ARCHITECTURE
```
User's Windows Machine (always on)
├── VS Code Terminal 1: python watchdog.py
│   └── watchdog.py auto-restarts bot.py on crash
│       ├── Kalshi LIVE API (authenticated)
│       ├── Coinbase WebSocket (real BTC/ETH)
│       ├── Polymarket Gamma API (public, read-only)
│       ├── 24+ RSS feeds + Brave Search
│       ├── Truth Social polling
│       ├── Flow Analyzer (CVD, VWAP, absorption)
│       ├── Edge Scanner (settlement, arb)
│       ├── Spread Reader (Poly vs Coinbase)
│       ├── Whale Tracker (Kalshi volume/price)
│       └── GHPages Publisher → state every 30s
│
├── VS Code Terminal 2: diagnostic commands
│
├── GitHub Pages: mikeytickets17.github.io/Kalshi/
│   ├── index.html (zero simulation, reads bot_state.json)
│   └── bot_state.json (pushed every 30s)
│
└── OpenClaw (installed, can monitor terminal)
```

## DIAGNOSTIC COMMANDS USER HAS RUN

### Check bot log:
```
python -c "lines=open('bot.log').readlines(); [print(l.strip()) for l in lines[-5:]]"
```

### Check Kalshi contracts fetched:
```
type bot.log | findstr "Fetched"
```

### Reset state file:
```
python -c "import json; s={'portfolio_value':10000,'initial_balance':10000,'peak_value':10000,'trade_count':0,'win_count':0,'active_positions':[],'closed_trades':[],'signals':[],'equity_curve':[10000],'trump_posts':[],'news_items':[],'risk':{},'whale_signals':[],'whale_copies':[],'start_time':0,'last_updated':0,'bot_running':False}; json.dump(s,open('bot_state.json','w')); print('RESET')"
```

### Explore Kalshi API:
```
python -c "from dotenv import load_dotenv; load_dotenv(); from pykalshi import KalshiClient; c=KalshiClient(demo=False); markets=c.get_markets(limit=1000); prefixes=sorted(set(m.ticker.split('-')[0] for m in markets if m.ticker)); print('Prefixes:',prefixes)"
```

### Check Kalshi events:
```
python -c "from dotenv import load_dotenv; load_dotenv(); from pykalshi import KalshiClient; c=KalshiClient(demo=False); events=c.get_events(limit=200); prefixes=sorted(set(e.event_ticker.split('-')[0] for e in events if hasattr(e,'event_ticker'))); print('Events:',prefixes)"
```

## DASHBOARD CONNECTION FLOW
1. Bot writes to bot_state.json every 3 seconds
2. Publisher reads bot_state.json every 30 seconds
3. Publisher sends to GitHub via Contents API with GITHUB_TOKEN
4. GitHub Pages serves updated index.html + bot_state.json
5. Browser fetches bot_state.json every 30 seconds
6. loadBotState() updates dashboard UI
7. Badge shows BOT CONNECTED when data is fresh (<300s old)

If dashboard says STANDALONE:
- Bot not running, or
- Publisher not working (check GITHUB_TOKEN in .env), or
- Browser localStorage has stale data (run `localStorage.clear()` in browser console F12)
