"""uvicorn entry point.

`python -m kalshi_arb.dashboard.main` for local dev.
Dockerfile uses `uvicorn kalshi_arb.dashboard.main:app --host 0.0.0.0 --port $PORT`.
"""

from __future__ import annotations

from .app import create_app

app = create_app()


if __name__ == "__main__":
    import os
    import uvicorn

    uvicorn.run(
        "kalshi_arb.dashboard.main:app",
        host="0.0.0.0",  # noqa: S104 -- bound 0.0.0.0 intentionally for container deploy
        port=int(os.environ.get("PORT", "8080")),
        reload=False,
        log_level="info",
    )
