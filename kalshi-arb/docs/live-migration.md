# Live-trading migration plan

v1 constraint: the bot runs on the operator's laptop during the 48h
paper-trading phase. When laptop is off, bot is off. Acceptable for
paper (operator commits to 48h continuous operation) but **NOT
acceptable for live trading** -- a closed laptop lid during a real
trading window means missed opportunities and, worse, naked positions
that can't be unwound.

This doc is the pre-written plan so we don't scramble at the live gate.

## Target architecture

```
┌─────────────────────┐       ┌─────────────────────┐
│ Fly machine #1      │       │ Fly machine #2      │
│ bot (kalshi-arb-bot)│ ─┐    │ dashboard           │
│ persistent volume   │ │libSQL│ embedded replica    │
└─────────────────────┘ │     └─────────────────────┘
                        │              ▲
                        ▼              │ pulls from primary
                ┌───────────────────────────┐
                │ Turso primary (managed)   │
                └───────────────────────────┘
```

Two separate Fly machines, same org, same region. Bot machine runs
`python -m kalshi_arb.cli ingest` + scanner + executor. Dashboard
machine runs the FastAPI app.

## Why two machines, not one

The dashboard is a pure read workload; the bot is compute-bound on
WS message dispatch and scanner ticks. Co-locating risks:

- SSE connection stalls during scanner bursts (the event loop is shared).
- A dashboard crash could take the bot down with it.
- Harder to blue/green redeploy (which matters once we're live).

Two machines keeps failures independent.

## Free-tier envelope check

Fly free tier: 3 × shared-cpu-1x-256MB machines. Two deployed = inside
the envelope. **Do not add a third** without reviewing whether another
module has also budgeted a machine.

## Pre-gate checklist (run this BEFORE flipping `LIVE_TRADING=true`)

1. **Prod probe on the bot machine**. Run
   `python -m kalshi_arb.probe.probe --env prod` on the Fly bot machine.
   Writes `config/detected_limits.yaml` with `environment: prod` and
   `ts_utc < 24h old`. Scanner refuses to load until this is fresh.
2. **Kalshi API key re-issued**. The demo key used during paper is
   revoked; a production key with IP allowlist matching the Fly bot
   machine's egress IP is configured.
3. **Fund the Kalshi account**. Dashboard's "starting balance" tile
   should show the actual deposit, not zero.
4. **Double-check `fly.toml` for bot machine**: `min_machines_running =
   1`, no auto-scale, no extra replicas, no region burst.
5. **Confirm degraded-mode monitor is wired**. Bot log should show
   periodic `degraded_mode.read_ok` entries with cash + positions
   consistent between reads.
6. **Rehearse kill-switch drop**. `fly ssh console -a kalshi-arb-bot`,
   `touch /app/KILL_SWITCH`, verify the bot logs `killswitch.tripped`
   within 1 second and the next `execute()` returns
   `outcome: kill_switch`.
7. **Pager path**. Until the SMS/email alert module ships, the
   operator commits to watching dashboard daily. The
   `CRITICAL_UNWIND_FAILED_*.txt` sentinel + kill switch is the only
   automated response; everything else requires human attention.
8. **Reconciliation-first live day protocol.** Day 1 of live trading is
   a controlled reconciliation exercise, not normal operation.

   - Reduce `HARD_CAP_USD` from whatever paper used (default $200) to
     **$25/trade** for day 1 only. Every other guardrail stays
     untouched.
   - Within 24 hours of each trade firing, operator manually reconciles
     it against Kalshi's settlement data: trade ID, fill prices, fees
     actually charged, settlement amount. Compare to the bot's
     `ExecutionResult.net_fill_cents` + `total_fees_cents` +
     `pnl_confidence`. **Every trade. Not a sample.**
   - The bug class this catches: paper P&L looked right but real
     settlement math differs (fee edge cases on 1¢ prices, rounding on
     partial fills, fee rebates we forgot about, Kalshi posting a
     trade-through correction).
   - Raise `HARD_CAP_USD` back to normal **only after** 100%
     reconciliation on day 1 passes with zero discrepancies > 1¢. If
     any trade is off by more than 1¢, halt, investigate, fix, then
     rerun day 1 at $25/trade until clean.
   - Log the reconciliation in `docs/reconciliation-log.md` (created
     at live gate) so there's a written record.

## Migration steps (do these at the gate, not before)

1. `fly launch --name kalshi-arb-bot --no-deploy` in a separate
   directory that contains a `Dockerfile` running
   `python -m kalshi_arb.cli ingest`.
2. `fly volumes create bot_data --size 1` for the PEM + local cache.
3. `fly secrets set KALSHI_API_KEY_ID=... KALSHI_PRIVATE_KEY_PATH=...
   LIBSQL_URL=... LIBSQL_AUTH_TOKEN=...` using the bot's token (see
   turso-setup.md).
4. `fly deploy`.
5. Watch `fly logs -a kalshi-arb-bot` for the first `scanner.emit` +
   `executor.both_filled` events. These should appear in the
   dashboard within 1s (SSE fan-out).
6. Turn off the laptop bot. Verify the change_log counter keeps
   advancing from the Fly-hosted bot only.

## Open questions for the live gate

- **Bot restart policy on crash**: Fly can auto-restart on exit.
  Confirm the existing watchdog logic (max 1000 restarts, 10s delay)
  matches Fly's behavior, or disable one of them -- two restart
  mechanisms compete.
- **Log retention**: Fly log output is ephemeral; ship structured
  logs to a file on the persistent volume + rotate.
- **Backup cadence**: Turso does point-in-time recovery but snapshot
  the DB nightly to the Fly volume anyway.

## Rollback from live to laptop-bot

If something goes wrong on live, the paper-phase setup is our
known-good baseline. Flip `LIVE_TRADING=false`, `fly scale count 0
-a kalshi-arb-bot`, restart the laptop watchdog. Dashboard keeps
working (it reads from Turso; doesn't care which bot is writing).
