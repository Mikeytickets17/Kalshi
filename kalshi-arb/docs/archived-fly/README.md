# Archived: Fly.io deployment artifacts

Kept intentionally. Not executed at this stage.

During Module 4 step 2 we built a full Fly.io deployment path
(Dockerfile, fly.toml pinned to free tier, walkthrough). We pivoted
to Cloudflare Tunnel for the paper phase because:

- No cloud signup / no credit card for v1
- Laptop is on for the 48h paper window anyway
- Dashboard reads the same local SQLite the bot writes -- no sync story
- If Fly tightens its free tier while we're paper-testing, we don't care

These artifacts land back in service at the live-trading gate, when the
bot has to run 24/7. See `docs/live-migration.md` for the migration plan
that uses them.

Files:
- `fly.toml` -- pinned for free tier. `[FREE-TIER-GUARD]` comments
  mark every setting that keeps us inside the free envelope. Do not
  edit those without review.
- `Dockerfile` -- single-stage Python 3.11 slim, non-root user,
  ~150 MB image. Good for both the dashboard and the bot.
- `deploy-dashboard.md` -- plain-English Windows walkthrough from
  `fly auth login` through the four gate checks.

No work is lost; restoring to active is a `git mv` plus reading the
walkthrough again.
