# kalshi-arb

Structural arbitrage engine for Kalshi prediction markets.

## Quick start -- dashboard

**Double-click `start_dashboard.bat`** (Windows) or run `./start_dashboard.sh`
(macOS / Linux). Wait ~10 seconds. The terminal will print a banner like:

```
======================================================================
  DASHBOARD URL:  https://<random-words>.trycloudflare.com
  LOGIN:          admin / <see .dashboard_creds>
  URL FILE:       /path/to/dashboard_url.txt
======================================================================
```

Open that URL in any browser (phone, laptop, anywhere). Log in with
username `admin` and the password in `.dashboard_creds` (generated on
first run, gitignored). Close the terminal or press `Ctrl+C` to stop
both the dashboard and the tunnel.

**What this uses:**
* Local FastAPI dashboard on `127.0.0.1:8000`.
* [cloudflared](https://github.com/cloudflare/cloudflared) in quick-tunnel
  mode -- no Cloudflare account needed. The binary is auto-downloaded
  to `bin/` on first run (~25 MB, one time).

**v1 constraint:** the bot and dashboard run on your laptop. When the
laptop is off, both stop. Acceptable for the 48 h paper phase. See
`docs/live-migration.md` for the plan to move to an always-on host
before live trading.

## What's in this directory

```
kalshi_arb/
  config.py               # env-driven typed config
  clock.py                # monotonic + wall clocks (epoch ms)
  log.py                  # structlog JSON with daily rotation
  rest/client.py          # REST facade over pykalshi
  ws/consumer.py          # sharded WebSocket consumer
  store/schema.sql        # SQLite schema (WAL)
  store/db.py             # async event store writer
  probe/probe.py          # 4-in-1 probe: WS cap + REST latency + ratelimit + E2E
  cli.py                  # `kalshi-arb probe` / `kalshi-arb ingest`
tests/                    # pytest suite
config/                   # detected_limits.yaml lands here after probe runs
data/                     # kalshi.db (gitignored)
logs/                     # daily-rotated JSON logs (gitignored)
```

## Setup (one time)

```bash
# 1. Install deps
cd kalshi-arb
pip install -e ".[dev]"

# 2. Copy env template
cp .env.example .env

# 3. Put your Kalshi demo API key + private key
#    (regenerate the production key; don't reuse the one on the main bot)
#    Edit .env:
#      KALSHI_API_KEY_ID=<your demo key id>
#      KALSHI_PRIVATE_KEY_PATH=./kalshi-demo.pem
#    Drop the PEM file next to this README.
```

## Run the probe (measures 4 unknowns)

Two modes. Demo first for local development; prod before the 48h paper run.

### Demo mode (safe, informational)

```bash
kalshi-arb probe --env demo
```

Writes `config/detected_limits.yaml` with `environment: demo`.
Demo market activity is too thin for the E2E loop, so that block is
marked `deferred`. Useful for local development; paper CLI will refuse
this file (it requires `environment: prod`).

### Prod mode (before the paper run)

```
Operator workflow -- double-click verify_prod_probe.bat
```

1. Get your production Kalshi API key + PEM file. In `.env`:
   ```
   KALSHI_USE_DEMO=false
   KALSHI_API_KEY_ID=<your production key id>
   KALSHI_PRIVATE_KEY_PATH=<path to your production PEM>
   ```
   Make sure you're on the IP that's allowlisted for your production key.
2. Double-click `verify_prod_probe.bat`. Wait up to ~3 minutes.
3. Read the popup:
   * **PASS** -- `config/detected_limits.yaml` is written with
     production numbers. Paper CLI's startup gate will now accept.
   * **FAIL** -- the popup names the specific failure. `detected_limits.yaml`
     is NOT written. Fix the issue and re-run.

What the prod probe does:

* Prints a 5-second countdown banner on startup (press Ctrl+C to abort).
* Runs 4 probes against production:
  1. WS subscription cap (how many tickers per connection)
  2. REST write latency p50 / p95 / p99 (100 samples)
  3. REST rate-limit ceiling (ramps to 40 rps or first 429)
  4. End-to-end arb loop latency (WS event -> REST fire, 30s window)
* Every order placed is a **1c BUY YES limit** tagged
  `probe-write-<ts>-<iter>` or `probe-e2e-<ts>-<iter>`, cancelled
  immediately. Cannot fill on any liquid market.
* Global 3-minute timeout. All-or-nothing file write: any failure
  aborts without writing `detected_limits.yaml` (partial numbers
  would poison the paper CLI gate).
* Acceptance thresholds (prod only):
  - WS: >= 50 tickers confirmed
  - REST write: >= 80% success rate with a p95
  - Rate limit: max_successful_rps >= 10
  - E2E: >= 5 samples

If `AUTO_PUBLISH=true` in `.env`, results commit to `kalshi-arb-data`
branch for remote review.

## Run the ingester (paper mode)

```bash
kalshi-arb ingest
```

- Discovers the universe (open markets in whitelisted categories, ≥ 24h
  volume threshold).
- Splits the universe into shards of `WS_MAX_TICKERS_PER_CONN` and opens one
  WebSocket per shard.
- Stores every orderbook delta, trade, and ticker event in `data/kalshi.db`.
- Per-shard staleness watchdog respawns any shard silent for >60s.
- Gap detection (missed `seq`) triggers a REST resnapshot.

## Tests

```bash
pytest tests/
```

## Migration to a standalone repo

This directory will migrate to its own GitHub repo via:

```bash
git subtree split --prefix=kalshi-arb -b kalshi-arb-standalone
# push kalshi-arb-standalone branch to the new repo's main
```

## What Push #1 does NOT do

- No scanner — detection of `yes_ask + no_ask < $1` opportunities comes in Push #2
- No sizer / executor — Push #3
- No dashboard — Push #4
- No backtest — Push #5
- No live trading — blocked by `LIVE_TRADING=false` until explicitly flipped
