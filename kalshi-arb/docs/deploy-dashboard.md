# Deploying the dashboard to Fly.io

Single-operator, free-tier deployment. Ten-minute first-time setup.
After this, every code change is `fly deploy` and takes ~90 seconds.

## Before you start

You need:
- A Fly.io account (free to create).
- A credit card on file — Fly requires it for abuse prevention even on
  free tier. We're going to set billing alerts below so a config drift
  can't silently spend money.
- 10 minutes.

## Step 1 — Install the Fly CLI

**Windows** (the bot laptop):

Open VS Code, **Terminal** → **New Terminal**. Paste this and hit Enter:

```
iwr https://fly.io/install.ps1 -useb | iex
```

Close the terminal and reopen it (so the new PATH is picked up). Verify:

```
fly version
```

You should see something like `fly v0.3.x`.

## Step 2 — Log in

In the same terminal:

```
fly auth login
```

A browser tab opens. Log in on Fly's website; the terminal will say "Successfully logged in" when done.

## Step 3 — Set billing alerts (do this first so surprises can't happen)

1. Open <https://fly.io/dashboard> in your browser.
2. Click your **org name** (top-left).
3. Click **Billing** in the sidebar.
4. Scroll to **Spending alerts**. Set three alerts:
   - `$1.00` (warning: something is off)
   - `$5.00` (investigate now)
   - `$10.00` (treat as an incident)
5. Add your email. Save.

If any one of those alerts fires, something in `fly.toml` drifted and we
need to review it. The committed `fly.toml` has `[FREE-TIER-GUARD]` comments
on every setting that matters — don't edit those lines without review.

## Step 4 — Create the app

Pull the latest code on the laptop:

```
cd C:\Users\mikey\Kalshi
git pull origin claude/fix-crypto-discovery-EvvsD
```

Then change into the kalshi-arb folder and launch the Fly app. The flags
matter — they tell Fly NOT to overwrite our pinned `fly.toml`:

```
cd kalshi-arb
fly launch --copy-config --no-deploy --name kalshi-arb-dashboard
```

Fly will ask a few questions:
- "Would you like to copy its configuration to the new app?" → **yes**
- "Do you want to tweak these settings before proceeding?" → **no**
- Choose the primary region → **iad** (US East — matches what's in `fly.toml`).
- "Would you like to set up a Postgresql database?" → **no**
- "Would you like to set up an Upstash Redis database?" → **no**

This creates the app shell but does NOT deploy yet.

## Step 5 — Set the dashboard password

Pick a strong password. Any 20+ character string works. Paste it into this
command (replace `REPLACE_THIS`):

```
fly secrets set DASHBOARD_PASSWORD=REPLACE_THIS --app kalshi-arb-dashboard
```

Also generate and set a session secret so sessions survive redeploys:

```
fly secrets set DASHBOARD_SESSION_SECRET=$(python -c "import secrets; print(secrets.token_urlsafe(48))") --app kalshi-arb-dashboard
```

On Windows PowerShell the `$()` syntax doesn't work. Use this instead:

```
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Copy the output, then:

```
fly secrets set DASHBOARD_SESSION_SECRET=<paste-the-output-here> --app kalshi-arb-dashboard
```

## Step 6 — Deploy

```
fly deploy --app kalshi-arb-dashboard
```

Takes ~90 seconds. Watch for `deployed successfully`.

Fly will print a URL like `https://kalshi-arb-dashboard.fly.dev`. Save it.

## Step 7 — Verify the four gate items

1. **Log in from your phone** via HTTPS:
   - Open the URL on your phone.
   - You'll see a login page. Username: `admin`. Password: what you set in step 5.
   - You should land on the **Overview** tab (stubbed).

2. **See all 6 tabs**: click each of the tab buttons in the top nav.
   All six (Overview, Opportunities, Trades Taken, P&L, System Health, News)
   should load (content is placeholder — that's step 5).

3. **`/healthz` returns 200**: open `https://kalshi-arb-dashboard.fly.dev/healthz`
   in the browser. You should see:
   ```json
   {"status": "ok", "version": "0.1.0", "step": 2}
   ```

4. **Billing alerts active**: go back to <https://fly.io/dashboard> →
   Billing → Spending alerts. Confirm the three thresholds from step 3
   are listed with your email attached.

## If something goes wrong

- `fly logs --app kalshi-arb-dashboard` shows the container output.
- `fly ssh console --app kalshi-arb-dashboard` drops you into the
  container for poking at files.
- `fly status --app kalshi-arb-dashboard` shows machine state.
- 99% of first-deploy failures are missing `DASHBOARD_PASSWORD`.
  The app refuses to start without it — check `fly secrets list`.

## What this deploy does NOT do

- No data layer connection yet. The tabs are empty stubs.
- No SSE. The footer says "SSE: not wired (step 3)" to make that obvious.
- No Turso database. We'll provision it in step 3.

When you've done the four gate checks, tell me. I'll land step 3 next.
