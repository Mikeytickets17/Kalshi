# CLAUDE.md — Memory File for Claude Code Sessions

## WHO IS THE USER
- Name: Mikey
- Machine: Windows PC at C:\Users\mikey\Kalshi
- Uses VS Code terminal (PowerShell)
- Does NOT want to touch code, terminal commands, or do anything technical
- Gets frustrated when asked to run commands — everything should be automated
- Python 3.14 installed via Python Install Manager, uses `py` not `python`
- Has Brave API key: BSAqXS8JxsYmDYyvOPUsog4WLEJ94qk (already in .env)
- Has Kalshi API key: d380c67d-9531-426a-b443-2eff3c5df967 (in .env)
- Kalshi private key file: C:\Users\mikey\Kalshi\mikebot (text file, not .pem)
- Currently running PAPER_MODE=true with KALSHI_USE_DEMO=true
- No other API keys configured yet (no Anthropic, Groq, Binance, Telegram)
- RUN.bat is the ONE file he double-clicks to start everything — never ask him to use terminal

## WHAT THIS PROJECT IS
- Automated Kalshi prediction market trading bot
- Monitors Trump social media + breaking news + whale activity
- Executes trades on Kalshi, Binance, Alpaca within seconds of market-moving events
- Goal: be FIRST before hedge funds and whales on every market-moving event
- Must use 100% REAL data — zero fake/simulated signals
- Paper mode = real signals, simulated execution (no real money yet)
- Dashboard at file:///C:/Users/mikey/Kalshi/dashboard.html
- Also works at http://localhost:5050 when dashboard.py is running

## TRADING STRATEGY: NEWS CHAIN REACTIONS
- Every headline triggers 5-8 trades across multiple markets
- chain_reactor.py maps event types to trade cascades:
  - Iran ceasefire → SHORT oil, LONG airlines, LONG stocks, LONG BTC, SHORT defense, LONG emerging
  - Rate cut → LONG BTC, LONG QQQ, LONG bonds, SHORT banks
  - PCE cool → LONG BTC, LONG QQQ, LONG SPY, LONG TLT
  - PCE hot → SELL everything
  - Tariffs → SHORT stocks, SHORT China (FXI), LONG gold, LONG agriculture
  - Oil crash → LONG airlines (JETS), LONG consumer, SHORT oil
  - Oil spike → LONG energy (XLE), SHORT airlines, SHORT consumer
  - Crypto bullish → LONG BTC, LONG ETH, LONG COIN, LONG MARA
  - Earnings beat → LONG specific stock + LONG SPY
- This is how hedge funds trade — capture every ripple effect from one event
- The bot trades the RIGHT asset for each headline, not just BTC on everything

## 6 TRADING STRATEGIES
1. TRUMP SOCIAL MEDIA — 8 sources (Truth Social API/RSS/Atom, Nitter x3, Twitter/X)
2. LATENCY ARB — CEX price vs Kalshi crypto contracts
3. BREAKING NEWS — 23+ RSS feeds + Brave Search + Reddit + Google News
4. KALSHI CONTRACT MATCHING — keyword match any event to Kalshi contracts
5. WHALE COPY TRADING — detect volume spikes, price jumps on Kalshi
6. EDGE DETECTION — contract mispricing, time decay, cross-platform arb

## SPEED OPTIMIZATIONS
- Trump poll interval: 500ms (was 3000ms)
- Keyword fast path: <100ms for obvious signals (tariff, ceasefire, bitcoin reserve, PCE)
- Zero wait time before trade execution (removed all 2-second delays)
- Total latency target: ~555ms from post to trade
- AI runs in background for additional multi-venue trades after fast trade
- PCE-specific fast path added for tomorrow 8:30 AM data release

## HOLD TIME SETTINGS
- Trump trades: 60 minutes (was 20)
- Arb trades: 30-90 minutes (was 5-15)
- News trades: 60 minutes (was 20)
- Max concurrent positions: 50 (was 5)
- Positions need time to breathe — small dips are noise, not losses

## KEY ARCHITECTURE DECISIONS
- Paper mode affects EXECUTION only, NOT signals — all data is real
- Trump monitor ALWAYS polls real Truth Social/Nitter (never fake paper posts)
- News feed uses ONLY real RSS (no paper news generation)
- Order book does NOT gate trades in paper mode
- Price feed tries real Binance/Coinbase first, paper fallback after 15s
- AI uses multi-provider: Anthropic → Groq → Gemini → Ollama → OpenRouter → rules
- Dashboard.py caches last good state — never flickers to zeros
- Dashboard.py serves ROOT dashboard.html (not templates/ copy)
- Bot state persists to bot_state.json, recovers portfolio on restart

## KALSHI ACCOUNT SETUP
- Account created at kalshi.com
- API Key ID: d380c67d-9531-426a-b443-2eff3c5df967
- Private key file: C:\Users\mikey\Kalshi\mikebot
- Currently: KALSHI_USE_DEMO=true (demo money)
- To go live: change KALSHI_USE_DEMO=false and PAPER_MODE=false in .env
- IMPORTANT: When PAPER_MODE=false, bot needs real account balance from Kalshi
  or portfolio shows $0. Keep PAPER_MODE=true until ready with funded account.

## KNOWN EDGE OPPORTUNITIES
- Cross-platform arb: Kalshi vs Polymarket price differences (same event)
- Combinatorial mispricing: 7,000+ markets with measurable mispricings
- Options-implied mispricing: SPX options vs Kalshi binary contracts
- News speed: Kalshi contracts take 1-2 min to reprice, bot trades in <1 sec
- Favorite-longshot bias: prices not perfectly calibrated (academic research)
- Kalshi fees: 3-7% on wins — must factor into edge calculation

## CURRENT MARKET CONTEXT (April 8, 2026)
- US-Iran 2-week ceasefire announced April 7 at 7:45 PM ET
- Dow +1,200 points (+2.6%), S&P +2.4%, Nasdaq +2.8%
- Oil crashed 17% from $113 to $93/barrel
- Peace talks Friday in Islamabad, VP Vance leading
- Trump posted 50% tariffs on Iran arms suppliers (8:02 AM April 8)
- Government shutdown day 53, DHS still unfunded
- BTC at ~$71,500, up on risk-on sentiment
- PCE inflation data drops tomorrow 8:30 AM — bot is ready

## NEXT STEPS TO LIVE CAPITAL
1. Run paper mode for 7 days to build track record
2. Show consistent positive P&L and win rate
3. Fund Kalshi account with starting capital
4. Switch KALSHI_USE_DEMO=false and PAPER_MODE=false
5. Start with small position sizes, scale up as track record proves out
6. Add Telegram alerts for real-time trade notifications on phone

## WINDOWS-SPECIFIC ISSUES FIXED
- asyncio.loop.add_signal_handler() crashes on Windows → platform check
- Emoji crash → forced UTF-8 on stdout/stderr
- RUN.bat saved as .txt → created RUN.ps1 as backup
- pip/python not found → use py

## DASHBOARD ISSUES FIXED
- Flask template wrong field names → rewrote template
- Dashboard flickered zeros → caches last good state
- localhost:5050 serves ROOT dashboard.html
- CORS headers added for file:// access
- Old P&L cached in localStorage → delete bot_state.json to reset

## FILES ON DISK
- RUN.bat / RUN.ps1 — launcher
- chain_reactor.py — maps headlines to multi-asset trade cascades
- edge_detector.py — finds contract mispricing and arb opportunities
- whale_tracker.py — detects smart money flow on Kalshi
- PROJECT_BRIEF.md — AI handoff document
- OVERNIGHT_REPORT.md — overnight trade report
- CLAUDE.md — this file
- bot_state.json — live bot state
- mikebot — Kalshi private key
- .env — API keys

## DO NOT
- Ask Mikey to run terminal commands
- Ask Mikey to edit code or pull from git
- Generate fake/simulated data
- Let the order book reject real signals in paper mode
- Use emojis in posts (Windows encoding crash)
- Use add_signal_handler on Windows
- Show stale cached data in dashboard
- Wait before executing trades
- Cut positions too early — let them breathe
- Trade only BTC on every headline — use chain reactions for right assets
- Forget that this sandbox blocks ALL outbound HTTP (403 on everything)

## FINAL SYSTEM DESIGN (CONFIRMED WITH USER)
- **KALSHI CONTRACTS ONLY** — no more Binance paper BTC trades, no Alpaca stock trades
- **$1,000 starting capital** when going live
- **Position sizing:** $50-100 per trade max
- **Risk:** Daily loss limit $100 (10%). Individual stop loss $15 per trade.
- **Small losses, big winners** — bot should judge when to cut vs hold
- **Brave Search is our AI** — no need for Groq/Anthropic
- **Binance account available** if needed (for BTC price feed, no key yet)
- **Alpaca available** if needed (not needed for Kalshi-only)
- **Regime detector active** — only trades in direction of macro trend
- **TACO detection** — Trump extreme threats = buy the fear
- **PCE fast path** — ready for tomorrow 8:30 AM
- **Dashboard must show ONLY real current data** — no cached old trades

## TRUMP BLUFF DETECTION (TACO PATTERN)
- Trump ALWAYS makes extreme threats before deals (100% historical rate)
- Wall Street calls it TACO: Trump Always Chickens Out
- Extreme language (destroy, obliterate, civilization will die) = deal incoming
- Strategy: BUY the fear when Trump threatens, sell when deal is announced
- Historical returns: -2% to -5% on threat, +3% to +9.5% on deal

## KEYWORD MARKET IMPACT DATA (20 Years Research)
- "tariff" = S&P -2.7% same day (proven by academic studies)
- "deal" / "pause" = S&P +1% to +9.5%
- "Don't worry" = futures +0.8% to +1.3%
- "ceasefire" = stocks +2-5%, oil -10-20% (INSTANT)
- "invasion" = stocks -5%, oil +20%
- Fed "patient" / "accommodative" = +1% to +2%
- Fed "restrictive" / "whatever it takes" = -3% to -5%
- PCE below expectations = BTC +3-5%, QQQ +1-2%
- PCE above expectations = BTC -3-5%, QQQ -1-2%
