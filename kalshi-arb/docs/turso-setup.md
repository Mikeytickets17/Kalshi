# Turso setup

Written for the live-migration architecture (not paper phase). Bot
writes go to the Turso primary; the always-on dashboard reads from an
embedded replica of the same database. Bot and dashboard use SEPARATE
auth tokens so either can be revoked without affecting the other.

Turso is NOT used in the paper phase -- the local SQLite event store
handles that fine and the bot + dashboard both run on the operator's
laptop. See `docs/live-migration.md` for when Turso becomes active.

## Python package install

libsql-experimental is an **optional** dependency so the paper-phase
operator doesn't need a Rust toolchain to install the repo. Install the
Turso extra explicitly when you're ready to wire up a live DB:

```bash
pip install -e ".[turso]"
```

Heads-up: on Python 3.14 Windows (and any other combo where no pre-built
wheel exists), this will pull down Rust to build the driver from source.
That's fine for a one-time install on the dashboard host; don't surprise
the paper-phase operator with it.

## One-time setup (Turso side)

1. Install the Turso CLI: <https://docs.turso.tech/cli/installation>.
2. Sign in: `turso auth login`.
3. Create the database (name it `kalshi-arb-prod`; region near the bot
   laptop -- e.g. `iad` for US East):

   ```bash
   turso db create kalshi-arb-prod --location iad
   ```

4. Get the primary URL (save this for both tokens):

   ```bash
   turso db show kalshi-arb-prod --url
   # libsql://kalshi-arb-prod-<org>.turso.io
   ```

5. Issue TWO auth tokens -- one per client, separately revocable:

   ```bash
   # Bot on laptop (writes): read-write, 30-day expiry.
   turso db tokens create kalshi-arb-prod --expiration 30d > bot.token

   # Dashboard on Fly (reads only; writes are blocked by the schema
   # not exposing mutating endpoints, but attach a read-only token
   # anyway for defense in depth).
   turso db tokens create kalshi-arb-prod --expiration 30d --read-only > dashboard.token
   ```

6. Put the tokens in the respective `.env` files:

   ```bash
   # On bot laptop:
   LIBSQL_URL=libsql://kalshi-arb-prod-<org>.turso.io
   LIBSQL_AUTH_TOKEN=<contents of bot.token>

   # On Fly (set via `fly secrets set`):
   LIBSQL_URL=libsql://kalshi-arb-prod-<org>.turso.io
   LIBSQL_AUTH_TOKEN=<contents of dashboard.token>
   LIBSQL_SYNC_URL=libsql://kalshi-arb-prod-<org>.turso.io
   LIBSQL_LOCAL_PATH=/data/replica.db
   ```

## Token rotation

When either token should be rotated (scheduled, or after suspected
leak):

```bash
# 1. List current tokens (copy the ID you want to revoke).
turso db tokens list kalshi-arb-prod

# 2. Revoke.
turso db tokens invalidate kalshi-arb-prod <token-id>

# 3. Issue a replacement.
turso db tokens create kalshi-arb-prod --expiration 30d > bot.token.new

# 4. Update the env var on the affected host.
#    Bot on laptop: edit .env, restart watchdog.
#    Dashboard: `fly secrets set LIBSQL_AUTH_TOKEN=<new>` -- Fly rolls the app.

# 5. Confirm writes / reads resume (check change_log row timestamps
#    updating + dashboard "Replica lag" tile stays <5s).
```

## Free-tier resource budget

Free tier: 500 DBs, 9 GB total storage, 1 B row reads/mo, 25 M row
writes/mo. Our expected load:

- Bot: ~100 writes/minute = 4.3 M writes/month. **17 % of free quota.**
- Dashboard: ~1 read/sec via polling + ad-hoc tab loads. ~2.5 M
  reads/month. **< 0.3 % of free quota.**

Over 5x safety margin on both axes. If usage creeps past 50 %, alert
the operator and consider tuning the dashboard poll cadence.

## What to check if replica lag stays high

1. Is the Fly machine reachable from Turso? `fly ssh console`, then
   `curl -I <LIBSQL_URL>`.
2. Has the auth token expired? `turso db tokens list` and the bot
   logs for `store.write_failed` bursts.
3. Is the embedded replica local file present? `ls -la /data/replica.db`
   inside the Fly machine.
4. If everything looks fine but lag is pinned at a high number, the
   dashboard's `sync()` task may have died. Check the FastAPI
   `/healthz` endpoint -- it includes replica-lag in the response body.

## Rollback plan (if Turso free tier changes or we need to leave)

Because the schema is pure SQLite, rollback is:

1. Download the latest `.db` dump: `turso db shell kalshi-arb-prod
   ".dump" > backup.sql`.
2. Restore locally: `sqlite3 /path/to/kalshi.db < backup.sql`.
3. Point the bot's `.env` at the local SQLite path by unsetting
   `LIBSQL_*` and setting `EVENT_STORE_PATH=/path/to/kalshi.db`.
4. Redeploy the dashboard pointing at a self-hosted libsql or a
   direct SQLite mount on the same Fly volume.

No code changes required -- that's what the `StoreBackend` abstraction
is for.
