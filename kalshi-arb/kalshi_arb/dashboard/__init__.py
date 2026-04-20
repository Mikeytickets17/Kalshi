"""Observability dashboard (Module 4).

FastAPI + SSE + HTMX + Chart.js + Tailwind (all via CDN). Read-only --
no UI control changes bot state; operator drives the bot via CLI.

Deployed to Fly.io on a single shared-cpu-1x-256MB machine within the
free tier. See fly.toml at repo root for the pinned config.

Skeleton (step 2) provides: auth, six stubbed tabs, /healthz. Step 3
wires Turso-change-capture + SSE. Step 5 fills tab content.
"""

__version__ = "0.1.0"
