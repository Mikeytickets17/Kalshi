"""FastAPI dashboard skeleton.

Step 2 scope (this file):
  * HTTP basic auth on every route except /healthz and /login
  * Signed session cookie after successful login
  * Six tab routes serving stubbed templates
  * /healthz returns 200 with a small JSON body
  * Rate limit on /login (60 req/min/IP, config-driven)

Out of scope for step 2 (landing in step 3):
  * Turso / event store wiring
  * SSE endpoint + change-capture fan-out
  * Tab content filled with real data

Security posture:
  * NO mutating endpoints. Only GET. This is enforced at router level --
    any POST/PUT/DELETE future addition must be reviewed for whether it
    changes bot state (which would violate the 'read-only' non-goal).
  * Cookies are signed + HttpOnly + Secure + SameSite=Lax.
  * Password comparison uses secrets.compare_digest (timing-safe).
"""

from __future__ import annotations

import secrets
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeSerializer

from .config import DashboardConfig
from .ratelimit import RateLimiter


_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"

SESSION_COOKIE = "kalshi_dash_session"
SESSION_MAX_AGE_SEC = 60 * 60 * 12  # 12h; operator re-auths daily

TABS = [
    ("overview", "Overview", True),   # default tab
    ("opportunities", "Opportunities", False),
    ("trades", "Trades Taken", False),
    ("pnl", "P&L", False),
    ("system-health", "System Health", False),
    ("news", "News", False),
]


def create_app(config: DashboardConfig | None = None) -> FastAPI:
    """Factory. Tests pass a custom config; production uses default env load."""
    cfg = config or DashboardConfig.load()
    app = FastAPI(
        title="kalshi-arb dashboard",
        version="0.1.0",
        # No /docs, /redoc, or /openapi.json -- dashboard is for humans
        # only and we don't want to leak an API map to anyone who hits
        # the URL without auth.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    # Session cookie signer. Any change to cfg.session_secret invalidates
    # all existing sessions (operator gets a forced logout).
    signer = URLSafeSerializer(cfg.session_secret, salt="kalshi-dash-session")

    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    # Make tab metadata available to every template without repeating it.
    templates.env.globals["TABS"] = TABS
    templates.env.globals["APP_VERSION"] = "0.1.0"

    if _STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    login_limiter = RateLimiter(max_per_min=cfg.login_rate_per_min_per_ip)

    # ---------------- auth helpers ----------------

    def _require_session(request: Request) -> None:
        """Raise 401 if no valid signed session cookie. Used as a
        dependency on every tab route."""
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            raise HTTPException(
                status_code=status.HTTP_303_SEE_OTHER,
                headers={"Location": "/login"},
            )
        try:
            data = signer.loads(token)
        except BadSignature:
            raise HTTPException(
                status_code=status.HTTP_303_SEE_OTHER,
                headers={"Location": "/login"},
            ) from None
        exp = int(data.get("exp", 0))
        if exp < int(time.time()):
            raise HTTPException(
                status_code=status.HTTP_303_SEE_OTHER,
                headers={"Location": "/login"},
            )

    def _issue_session() -> str:
        return signer.dumps({
            "user": cfg.username,
            "exp": int(time.time()) + SESSION_MAX_AGE_SEC,
            # Random nonce so two logins from the same operator don't
            # produce an identical cookie.
            "nonce": secrets.token_urlsafe(12),
        })

    # ---------------- routes ----------------

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        # Step 2: returns 200 unless the process is wedged. Step 3 will
        # expand this with DB connection check + SSE listener health +
        # replica lag.
        return JSONResponse({
            "status": "ok",
            "version": "0.1.0",
            "step": 2,
        })

    @app.get("/", include_in_schema=False)
    async def root(request: Request) -> RedirectResponse:
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            try:
                signer.loads(token)
                return RedirectResponse("/overview", status_code=303)
            except BadSignature:
                pass
        return RedirectResponse("/login", status_code=303)

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request, error: str | None = None) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": error},
        )

    @app.post("/login")
    async def login_submit(request: Request) -> RedirectResponse:
        ip = _client_ip(request)
        if not login_limiter.allow(ip):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many login attempts. Wait a minute and try again.",
            )
        form = await request.form()
        submitted_user = str(form.get("username", ""))
        submitted_pw = str(form.get("password", ""))

        user_ok = secrets.compare_digest(submitted_user, cfg.username)
        pw_ok = secrets.compare_digest(submitted_pw, cfg.password)
        if not (user_ok and pw_ok):
            # Don't leak which side of the pair was wrong.
            return RedirectResponse("/login?error=invalid", status_code=303)

        resp = RedirectResponse("/overview", status_code=303)
        resp.set_cookie(
            key=SESSION_COOKIE,
            value=_issue_session(),
            max_age=SESSION_MAX_AGE_SEC,
            httponly=True,
            secure=True,
            samesite="lax",
        )
        return resp

    @app.get("/logout")
    async def logout() -> RedirectResponse:
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(SESSION_COOKIE)
        return resp

    # --- Tab routes. All require auth. Content is a stub in step 2. ---

    def _tab(name: str, title: str):
        async def _handler(
            request: Request, _=Depends(_require_session)
        ) -> HTMLResponse:
            return templates.TemplateResponse(
                request=request,
                name=f"tabs/{name}.html",
                context={
                    "active_tab": name,
                    "tab_title": title,
                },
            )
        _handler.__name__ = f"tab_{name.replace('-', '_')}"
        return _handler

    for slug, title, _default in TABS:
        app.get(f"/{slug}", response_class=HTMLResponse)(_tab(slug, title))

    return app


def _client_ip(request: Request) -> str:
    """Best-effort remote IP. Behind Fly's proxy, the real client IP is
    in the Fly-Client-IP header; fall back to the direct socket peer."""
    return (
        request.headers.get("fly-client-ip")
        or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        or (request.client.host if request.client else "unknown")
    )
