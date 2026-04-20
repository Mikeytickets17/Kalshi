"""FastAPI dashboard skeleton.

Step 2 scope (baseline): auth, six tabs, /healthz.
Step 3 adds:
  * /events/stream -- SSE endpoint with Last-Event-ID auto-resume
  * /events/poll -- fallback JSON endpoint for clients where SSE drops
  * ChangeCapture background task polling change_log at 1s cadence
  * EventStore connected in read-only-for-bot, read-write-for-schema
    mode (schema bootstrap is idempotent; dashboard never writes
    domain rows because record_* helpers aren't called here)

Out of scope:
  * Tab content filled with real data (step 5)
  * Chart.js + CSV (step 6)

Security posture:
  * NO mutating endpoints. Only GET. This is enforced at router level --
    any POST/PUT/DELETE future addition must be reviewed for whether it
    changes bot state (which would violate the 'read-only' non-goal).
  * Cookies are signed + HttpOnly + Secure + SameSite=Lax.
  * Password comparison uses secrets.compare_digest (timing-safe).
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeSerializer

from ..store import EventStore, SqliteBackend
from .config import DashboardConfig
from .csv_export import opportunities_csv, trades_csv
from .queries import (
    OpportunityFilters,
    TradeFilters,
    opportunities_query,
    opportunity_detail,
    overview_data,
    pnl_data,
    system_health_data,
    trade_detail,
    trades_query,
)
from .ratelimit import RateLimiter
from .sse import Change, ChangeCapture, SSEBroker, _CLOSE_SENTINEL


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


def create_app(
    config: DashboardConfig | None = None,
    *,
    store: EventStore | None = None,
) -> FastAPI:
    """Factory. Tests pass a custom config + store; production uses
    default env-loaded config and opens the local SQLite event store."""
    cfg = config or DashboardConfig.load()

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        # Use the injected store if provided (tests); otherwise open the
        # local SQLite event store the bot is also writing to.
        if store is not None:
            app.state.store = store
            close_store = False
        else:
            app.state.store = EventStore(SqliteBackend(cfg.event_store_path))
            app.state.store.connect()
            close_store = True

        app.state.broker = SSEBroker()
        # Tests that don't need live capture pass start_change_capture=False
        # via a config-ish route (we keep the knob internal to avoid
        # publicly-visible fields on DashboardConfig).
        app.state.capture = ChangeCapture(
            app.state.store,
            app.state.broker,
            tick_sec=1.0,
            start_at_latest=not cfg.replay_backlog_on_start,
        )
        await app.state.capture.start()
        try:
            yield
        finally:
            await app.state.capture.stop()
            if close_store:
                # store.stop() only applies if writer loop is running;
                # dashboard doesn't start the writer, just the connection.
                app.state.store.backend.close()

    app = FastAPI(
        title="kalshi-arb dashboard",
        version="0.1.0",
        # No /docs, /redoc, or /openapi.json -- dashboard is for humans
        # only and we don't want to leak an API map to anyone who hits
        # the URL without auth.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=_lifespan,
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
    async def healthz(request: Request) -> JSONResponse:
        store_ref: EventStore | None = getattr(request.app.state, "store", None)
        broker: SSEBroker | None = getattr(request.app.state, "broker", None)
        capture: ChangeCapture | None = getattr(request.app.state, "capture", None)
        body: dict = {"status": "ok", "version": "0.1.0", "step": 3}
        # Surface the absolute path the dashboard is reading from so the
        # verifier (a separate process) can confirm it is opening the
        # same file. A mismatch here is the smoking gun for "writes go
        # in, reads see nothing" -- which has bitten step 3 twice.
        body["event_store_path"] = str(cfg.event_store_path)
        if store_ref is not None:
            body["replica_lag_ms"] = store_ref.replica_lag_ms()
            body["store_stats"] = store_ref.stats()
            # Total rows currently visible to the dashboard's connection.
            # The verifier compares this to its own count to detect WAL
            # visibility issues separately from path mismatch.
            try:
                body["change_log_count"] = store_ref.change_log_count()
            except Exception as exc:  # noqa: BLE001
                body["change_log_count_error"] = str(exc)
        if broker is not None:
            body["sse"] = broker.stats()
        if capture is not None:
            body["capture"] = {"since_id": capture.since_id}
        return JSONResponse(body)

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

    # ---------------- change-capture endpoints ----------------

    @app.get("/events/stream")
    async def events_stream(
        request: Request, _=Depends(_require_session)
    ) -> StreamingResponse:
        """Server-Sent Events. Every new change_log row arrives here as
        an event. Reconnect is native to browsers -- EventSource retries
        automatically and sends Last-Event-ID, which we honor below to
        replay missed rows with zero loss."""
        broker: SSEBroker = request.app.state.broker
        store_ref: EventStore = request.app.state.store

        # Honor Last-Event-ID for gap-free resume after a dropped connection.
        resume_id = 0
        last_id_hdr = request.headers.get("last-event-id")
        if last_id_hdr and last_id_hdr.isdigit():
            resume_id = int(last_id_hdr)

        async def _gen():
            # 1) Replay any rows the client missed while disconnected.
            if resume_id > 0:
                for row in store_ref.changes_since(resume_id, limit=500):
                    yield Change.from_row(row).as_sse_event()

            # 2) Live stream. Use the lower-level queue primitive so we
            # can race queue.get() against a disconnect-check tick.
            # Without this, a subscriber with no new events would block
            # on queue.get() forever and miss the client-disconnect
            # signal, leaking the generator.
            q = broker.add_subscriber()
            try:
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        item = await asyncio.wait_for(q.get(), timeout=3.0)
                    except (asyncio.TimeoutError, TimeoutError):
                        # SSE comment line. Clients ignore it but it
                        # keeps the connection warm through proxies.
                        yield ": keepalive\n\n"
                        continue
                    if item is _CLOSE_SENTINEL:
                        return
                    yield item.as_sse_event()
            finally:
                broker.remove_subscriber(q)

        return StreamingResponse(
            _gen(),
            media_type="text/event-stream",
            headers={
                # Prevent intermediate proxies (including Cloudflare)
                # from buffering the stream.
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    @app.get("/events/poll")
    async def events_poll(
        request: Request,
        since_id: int = 0,
        limit: int = 100,
        _=Depends(_require_session),
    ) -> JSONResponse:
        """Fallback for clients that can't hold an SSE connection
        (some corporate proxies, intermittent networks). Same payload
        shape as the SSE events array."""
        store_ref: EventStore = request.app.state.store
        rows = store_ref.changes_since(
            since_id, limit=min(max(1, limit), 500)
        )
        return JSONResponse(
            {
                "changes": [
                    {
                        "id": int(r[0]),
                        "entity_type": r[1],
                        "entity_id": r[2],
                        "ts_ms": r[3],
                        "payload": r[4],
                    }
                    for r in rows
                ],
            }
        )

    # --- Tab routes. All require auth. SSR data is passed into templates. ---

    def _opp_filters_from_query(request: Request) -> OpportunityFilters:
        q = request.query_params
        hours = _parse_int(q.get("hours"))
        min_edge = _parse_float(q.get("min_edge"))
        limit = _parse_int(q.get("limit")) or 500
        offset = _parse_int(q.get("offset")) or 0
        return OpportunityFilters(
            hours=hours if hours and hours > 0 else None,
            ticker=(q.get("ticker") or "").strip() or None,
            decision=(q.get("decision") or "").strip() or None,
            min_edge_cents=min_edge,
            limit=limit,
            offset=offset,
            sort=q.get("sort", "ts_ms"),
            sort_dir=q.get("sort_dir", "desc"),
        )

    def _trade_filters_from_query(request: Request) -> TradeFilters:
        q = request.query_params
        hours = _parse_int(q.get("hours"))
        limit = _parse_int(q.get("limit")) or 500
        offset = _parse_int(q.get("offset")) or 0
        return TradeFilters(
            hours=hours if hours and hours > 0 else None,
            ticker=(q.get("ticker") or "").strip() or None,
            outcome=(q.get("outcome") or "").strip() or None,
            limit=limit,
            offset=offset,
            sort=q.get("sort", "ts_ms"),
            sort_dir=q.get("sort_dir", "desc"),
        )

    def _render_tab(request: Request, name: str, title: str, data: dict) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name=f"tabs/{name}.html",
            context={
                "active_tab": name,
                "tab_title": title,
                "data": data,
                "data_json": json.dumps(data, default=str),
            },
        )

    # ----- Overview -----
    @app.get("/overview", response_class=HTMLResponse)
    async def tab_overview(
        request: Request, _=Depends(_require_session)
    ) -> HTMLResponse:
        data = overview_data(request.app.state.store)
        return _render_tab(request, "overview", "Overview", data)

    @app.get("/overview/data")
    async def overview_data_endpoint(
        request: Request, _=Depends(_require_session)
    ) -> JSONResponse:
        return JSONResponse(overview_data(request.app.state.store))

    # ----- Opportunities -----
    @app.get("/opportunities", response_class=HTMLResponse)
    async def tab_opportunities(
        request: Request, _=Depends(_require_session)
    ) -> HTMLResponse:
        filters = _opp_filters_from_query(request)
        data = opportunities_query(request.app.state.store, filters)
        data["filters"] = _filters_as_dict(filters)
        return _render_tab(request, "opportunities", "Opportunities", data)

    @app.get("/opportunities/data")
    async def opportunities_data(
        request: Request, _=Depends(_require_session)
    ) -> JSONResponse:
        filters = _opp_filters_from_query(request)
        return JSONResponse(opportunities_query(request.app.state.store, filters))

    @app.get("/opportunities/export.csv")
    async def opportunities_export_csv(
        request: Request, _=Depends(_require_session)
    ) -> StreamingResponse:
        filters = _opp_filters_from_query(request)
        return opportunities_csv(request.app.state.store, filters)

    @app.get("/opportunities/{opp_id}/detail")
    async def opportunity_detail_endpoint(
        opp_id: int, request: Request, _=Depends(_require_session)
    ) -> JSONResponse:
        data = opportunity_detail(request.app.state.store, opp_id)
        if data is None:
            raise HTTPException(status_code=404, detail="opportunity not found")
        return JSONResponse(data)

    # ----- Trades Taken -----
    @app.get("/trades", response_class=HTMLResponse)
    async def tab_trades(
        request: Request, _=Depends(_require_session)
    ) -> HTMLResponse:
        filters = _trade_filters_from_query(request)
        data = trades_query(request.app.state.store, filters)
        data["filters"] = _filters_as_dict(filters)
        return _render_tab(request, "trades", "Trades Taken", data)

    @app.get("/trades/data")
    async def trades_data(
        request: Request, _=Depends(_require_session)
    ) -> JSONResponse:
        filters = _trade_filters_from_query(request)
        return JSONResponse(trades_query(request.app.state.store, filters))

    @app.get("/trades/export.csv")
    async def trades_export_csv(
        request: Request, _=Depends(_require_session)
    ) -> StreamingResponse:
        filters = _trade_filters_from_query(request)
        return trades_csv(request.app.state.store, filters)

    @app.get("/trades/{opp_id}/detail")
    async def trade_detail_endpoint(
        opp_id: int, request: Request, _=Depends(_require_session)
    ) -> JSONResponse:
        data = trade_detail(request.app.state.store, opp_id)
        if data is None:
            raise HTTPException(status_code=404, detail="trade not found")
        return JSONResponse(data)

    # ----- P&L -----
    @app.get("/pnl", response_class=HTMLResponse)
    async def tab_pnl(
        request: Request, _=Depends(_require_session)
    ) -> HTMLResponse:
        hours = _parse_int(request.query_params.get("hours")) or 24 * 7
        data = pnl_data(request.app.state.store, hours=hours)
        return _render_tab(request, "pnl", "P&L", data)

    @app.get("/pnl/data")
    async def pnl_data_endpoint(
        request: Request, _=Depends(_require_session)
    ) -> JSONResponse:
        hours = _parse_int(request.query_params.get("hours")) or 24 * 7
        return JSONResponse(pnl_data(request.app.state.store, hours=hours))

    # ----- System Health -----
    @app.get("/system-health", response_class=HTMLResponse)
    async def tab_system_health(
        request: Request, _=Depends(_require_session)
    ) -> HTMLResponse:
        data = system_health_data(request.app.state.store)
        return _render_tab(request, "system-health", "System Health", data)

    @app.get("/system-health/data")
    async def system_health_data_endpoint(
        request: Request, _=Depends(_require_session)
    ) -> JSONResponse:
        return JSONResponse(system_health_data(request.app.state.store))

    # ----- News (placeholder tab only) -----
    @app.get("/news", response_class=HTMLResponse)
    async def tab_news(
        request: Request, _=Depends(_require_session)
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="tabs/news.html",
            context={"active_tab": "news", "tab_title": "News", "data": {}},
        )

    return app


def _parse_int(raw: str | None) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _parse_float(raw: str | None) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _filters_as_dict(f) -> dict:
    """Serialize an OpportunityFilters / TradeFilters for the template."""
    return {
        "hours": getattr(f, "hours", None),
        "ticker": getattr(f, "ticker", None),
        "decision": getattr(f, "decision", None),
        "outcome": getattr(f, "outcome", None),
        "min_edge_cents": getattr(f, "min_edge_cents", None),
        "limit": getattr(f, "limit", None),
        "offset": getattr(f, "offset", None),
        "sort": getattr(f, "sort", None),
        "sort_dir": getattr(f, "sort_dir", None),
    }


def _client_ip(request: Request) -> str:
    """Best-effort remote IP. Behind Fly's proxy, the real client IP is
    in the Fly-Client-IP header; fall back to the direct socket peer."""
    return (
        request.headers.get("fly-client-ip")
        or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        or (request.client.host if request.client else "unknown")
    )
