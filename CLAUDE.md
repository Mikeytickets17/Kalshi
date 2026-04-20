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
- Executes trades on Kalshi within seconds of market-moving events
- Goal: be FIRST before hedge funds and whales on every market-moving event
- Must use 100% REAL data — zero fake/simulated signals
- Paper mode = real signals, simulated execution (no real money yet)
- Dashboard at file:///C:/Users/mikey/Kalshi/kalshi_dashboard.html
- Also works at http://localhost:5050 when dashboard.py is running

## CURRENT FOCUS: BTC 15-MINUTE CONTRACTS ONLY
- ALL other strategies disabled until BTC is perfected
- btc_trader.py is the ONLY active strategy
- Gets real BTC price from Binance (free, no key)
- Calculates 60-second momentum
- If momentum > +0.15% → BUY YES (price going up)
- If momentum < -0.15% → BUY NO (price going down)
- Trades fire every 15 minutes, 24/7
- $75 per trade, $100 daily loss limit
- Dashboard: kalshi_dashboard.html (new clean version)

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

## DISABLED STRATEGIES (saved for later)
- Strategy 1: Latency Arb (price_feed, scanner, signal_processor)
- Strategy 2: Trump News (trump_monitor, trump_news_processor)
- Strategy 3: Breaking News (news_feed, news_processor)
- Strategy 4: Kalshi Contract Matching (contract_matcher)
- Strategy 5: Whale Copy Trading (whale_tracker)
- Strategy 6: Edge Detection (edge_detector)
- All code still exists, just not running in tasks list
- Re-enable after BTC 15-min is profitable

## RESEARCH: HOW TRADERS TURNED SMALL ACCOUNTS INTO MILLIONS
- French Whale: $30M → $85M on Trump election (private polls = better info)
- Iran Trader: $0 → $967K, 93% win rate (insider or speed edge)
- @theduckguesses: $100 → $145K on Kalshi (compound growth)
- Caleb Davies: $389K in culture markets (domain expertise + data models)
- Logan Sudeith: $100K in one month (high-volume culture trades)
- Key pattern: INFORMATION EDGE + COMPOUND GROWTH + DOMAIN EXPERTISE
- Kelly Criterion: bet proportional to edge, half-Kelly for safety
- See RESEARCH_MILLIONAIRE_TRADERS.md for full analysis

## COMPOUND GROWTH TARGET
- $1,000 → $10,000 in 30 days (10x)
- $10,000 → $100,000 in 90 days (10x)
- $100,000 → $1,000,000 in 180 days (10x)
- Requires: 60%+ win rate, 2:1 payout ratio, reinvesting all profits

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

## PCE DATA — April 9, 2026 at 8:30 AM
- Expected: Core PCE 2.5% annual, 0.12% monthly
- If below → BUY (rate cuts coming)
- If above → SELL (no rate cuts)
- Bot has fast path detection for PCE keywords
- Iran ceasefire dropped oil 17% → removes inflation pressure → likely cool PCE

## KALSHI ACCOUNT SETUP
- Account created at kalshi.com
- API Key ID: d380c67d-9531-426a-b443-2eff3c5df967
- Private key file: C:\Users\mikey\Kalshi\mikebot
- Currently: KALSHI_USE_DEMO=true (demo money)
- To go live: change KALSHI_USE_DEMO=false and PAPER_MODE=false in .env
- IMPORTANT: When PAPER_MODE=false, bot needs real account balance from Kalshi
  or portfolio shows $0. Keep PAPER_MODE=true until ready with funded account.

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

## EXIT RULES
- Take profit: +$50
- Stop loss: -$15
- Time exit: 60 minutes
- No more random coin-flip exits
- No more closing $900 positions for $1 loss

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
- NEW: kalshi_dashboard.html — clean Kalshi-only dashboard

## FILES ON DISK
- RUN.bat / RUN.ps1 — launcher
- btc_trader.py — 15-minute BTC Kalshi contract trader
- chain_reactor.py — maps headlines to multi-asset trade cascades
- edge_detector.py — finds contract mispricing and arb opportunities
- whale_tracker.py — detects smart money flow on Kalshi
- regime_detector.py — picks market direction, stops trading both sides
- kalshi_dashboard.html — NEW clean Kalshi-only dashboard
- RESEARCH_MILLIONAIRE_TRADERS.md — how traders turned small accounts into millions
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
- Run multiple strategies at once until BTC is perfected
- Show SPY, USO, LMT, IRAN, GOV-SHUTDOWN trades — BTC ONLY right now
