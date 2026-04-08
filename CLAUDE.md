# CLAUDE.md — Memory File for Claude Code Sessions

## WHO IS THE USER
- Name: Mikey
- Machine: Windows PC at C:\Users\mikey\Kalshi
- Uses VS Code terminal (PowerShell)
- Does NOT want to touch code, terminal commands, or do anything technical
- Gets frustrated when asked to run commands — everything should be automated
- Python 3.14 installed via Python Install Manager, uses `py` not `python`
- Has Brave API key: BSAqXS8JxsYmDYyvOPUsog4WLEJ94qk (already in .env)
- No other API keys configured yet (no Kalshi, Anthropic, Groq, Binance, Telegram)
- RUN.bat is the ONE file he double-clicks to start everything — never ask him to use terminal

## WHAT THIS PROJECT IS
- Automated Kalshi prediction market trading bot
- Monitors Trump social media + breaking news + whale activity
- Executes trades on Kalshi, Binance, Alpaca within seconds of market-moving events
- Goal: be FIRST before hedge funds and whales on every market-moving event
- Must use 100% REAL data — zero fake/simulated signals
- Paper mode = real signals, simulated execution (no real money yet)
- Dashboard at file:///C:/Users/mikey/Kalshi/dashboard.html

## 6 TRADING STRATEGIES
1. TRUMP SOCIAL MEDIA — 8 sources (Truth Social API/RSS/Atom, Nitter x3, Twitter/X)
2. LATENCY ARB — CEX price vs Kalshi crypto contracts
3. BREAKING NEWS — 23+ RSS feeds + Brave Search + Reddit + Google News
4. KALSHI CONTRACT MATCHING — keyword match any event to Kalshi contracts
5. WHALE COPY TRADING — detect volume spikes, price jumps on Kalshi
6. EDGE DETECTION — contract mispricing, time decay, cross-platform arb

## KEY ARCHITECTURE DECISIONS
- Paper mode affects EXECUTION only, NOT signals — all data is real
- Trump monitor ALWAYS polls real Truth Social/Nitter (never fake paper posts)
- News feed uses ONLY real RSS (no paper news generation)
- Order book does NOT gate trades in paper mode (was rejecting 90% of real signals)
- Price feed tries real Binance/Coinbase first, paper fallback after 15s only if real fails
- AI uses multi-provider: Anthropic → Groq → Gemini → Ollama → OpenRouter → rules fallback
- Dashboard connects to bot via localhost:5050/api/state with CORS headers
- file:// dashboard works because fetch URL is http://localhost:5050/api/state (not relative)
- Bot state persists to bot_state.json, recovers portfolio on restart
- Research scanner saves to research_log.json, runs Brave Search every 30min

## WINDOWS-SPECIFIC ISSUES FIXED
- asyncio.loop.add_signal_handler() crashes on Windows → platform check added
- Emoji in Trump posts crash Windows console (cp1252 encoding) → forced UTF-8 on stdout/stderr
- `./start.sh` doesn't work on Windows → created RUN.bat and start.bat
- `pip` not found → use `py -m pip`
- `python` not found → use `py`
- Git branch had slashes which Windows handles badly → created clean `main` branch

## CRITICAL BUGS THAT WERE FIXED
- Kalshi order size was 10x too large (count = size_usd / price, wrong math)
- Trump exit P&L was $0 for all Kalshi contracts (used qty=0 instead of size_usd)
- NO-side stop loss never triggered (formula inverted)
- News exit P&L used random.uniform() even in live mode (now uses real fill prices)
- CPI classification missed "US CPI comes in at" (added standalone " cpi " keyword)
- Sentiment direction case mismatch (BULLISH vs bullish) across modules
- Order book direction matching was case-sensitive
- Paper mode win probability was backwards (high price = high win, should be opposite)
- Price feed confidence too harsh (2% spread = 0 confidence, now uses spread*20)
- Failed exit orders retried infinitely (added 3-retry max)
- yes_ask=0 treated as falsy in Kalshi parser (now checks `is not None`)
- Config TARGET_ASSETS didn't strip whitespace
- News items weren't persisted to disk in shared_state
- record_news() missing _persist() call

## DASHBOARD ISSUES FIXED
- Flask template had wrong field names (ticker/type vs strategy/asset) → rewrote template
- Dashboard flickered between real data and zeros → bot_state.json was being read during write
- Fix: dashboard.py now caches last good state, never returns zeros if bot is running
- localhost:5050 serves from templates/dashboard.html (new version with correct field mapping)
- file:///C:/Users/mikey/Kalshi/dashboard.html is the v5.1 dashboard (works standalone)
- The file:// dashboard connects to localhost:5050/api/state for bot data
- CORS headers added via @app.after_request so file:// can reach localhost
- If bot not running, file:// dashboard shows news but no trades (correct behavior)

## KALSHI ACCOUNT SETUP (NEXT STEP)
- Go to https://kalshi.com/sign-up
- Create account, verify identity
- Go to Settings > API
- Create API key → get Key ID + download .pem private key file
- Save .pem file to C:\Users\mikey\Kalshi\
- Edit .env file and add:
  KALSHI_API_KEY_ID=your-key-id
  KALSHI_PRIVATE_KEY_PATH=./your-key-file.pem
  KALSHI_USE_DEMO=true (start with demo, switch to false for real money)
  PAPER_MODE=false (when ready for real trading)

## FILES ON DISK
- RUN.bat — ONE double-click starts everything (auto-updates, starts bot+dashboard, opens browser)
- PROJECT_BRIEF.md — full project description for AI handoff
- OVERNIGHT_REPORT.md — real-time trading report from overnight monitoring
- overnight_dashboard.html — visual report of overnight trades
- CLAUDE.md — this file, persistent memory across sessions
- bot_state.json — live bot state (portfolio, trades, positions)
- research_log.json — Brave Search research findings
- bot.log — full activity log
- .env — API keys (Brave key already set)

## WHAT USER WANTS NEXT
- Bot running 24/7 on his machine (just double-click RUN.bat)
- Real trades on Kalshi with real money (needs Kalshi API key)
- Telegram alerts on his phone for every trade
- Never touch code or terminal again
- Overnight reports showing real trades based on real events
- The bot to catch EVERY market-moving event: Iran, Russia, NATO, tariffs, Fed, crypto, Trump posts
- Find loopholes in prediction markets (contract mispricing, cross-platform arb, time decay)

## REPO
- GitHub: https://github.com/Mikeytickets17/Kalshi
- Default branch: main
- All merges go to main
- RUN.bat auto-pulls from main on every launch

## DO NOT
- Ask Mikey to run terminal commands
- Ask Mikey to edit code
- Ask Mikey to pull from git manually
- Generate fake/simulated data
- Show paper mode fake Trump posts or fake news headlines
- Let the order book reject real signals in paper mode
- Use emojis in paper mode posts (Windows encoding crash)
- Use add_signal_handler on Windows
- Forget that this sandbox blocks ALL outbound HTTP (403 on everything)
