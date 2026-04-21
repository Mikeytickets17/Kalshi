"""Integration tests that exercise the REAL pykalshi 1.0.4 stack.

Methodology post-mortem:

PRs #11, #12, #13, and #14 each declared the probe "sandbox-verified"
and each one failed on the operator's first real-prod run. The common
flaw: those tests asserted against fakes the author wrote. Fakes
return what the author thinks pykalshi returns; the real library
returns something different, and we only find out when real prod
runs.

This file breaks the loop. It constructs REAL pykalshi KalshiClient
and AsyncKalshiClient instances (with a throwaway RSA key so signing
succeeds), plugs `httpx.MockTransport` in at the HTTP layer so no
real network call fires, and drives the entire pykalshi codepath
(validation -> _build_order_data -> .value dereferences -> request
signing -> response parsing -> Order model hydration). If ANY of
that crashes in the sandbox, the test catches it.

If any future PR re-introduces a string-where-enum-required bug
(see: PR #14's `.value on str` crash on prod), these tests will
fail in CI BEFORE the PR merges.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import tempfile
from typing import Any

import httpx
import pytest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


# ----- throwaway RSA key so pykalshi signing doesn't crash ----------


@pytest.fixture(scope="module")
def throwaway_pem() -> str:
    """Generate an RSA-2048 PEM. pykalshi's _load_private_key uses
    cryptography.hazmat.primitives.serialization.load_pem_private_key
    so any valid unencrypted PEM works."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem_bytes = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    with tempfile.NamedTemporaryFile(
        mode="wb", suffix=".pem", delete=False
    ) as f:
        f.write(pem_bytes)
        path = f.name
    yield path
    os.unlink(path)


# ----- canned responses matching Kalshi's real wire format ----------


def _order_response_body(
    *,
    order_id: str = "ord-abc-123",
    fill_count_fp: str = "0",
    status: str = "resting",
    ticker: str = "KXBTC-TEST",
    client_order_id: str = "probe-write-1-0",
) -> bytes:
    """A real Kalshi POST /portfolio/orders response body.

    Verified against pykalshi's OrderModel definition in
    /usr/local/lib/.../pykalshi/models.py. Fields that exist are
    populated; fields that don't are absent (Pydantic uses extra='ignore').
    """
    return json.dumps({
        "order": {
            "order_id": order_id,
            "ticker": ticker,
            "status": status,
            "action": "buy",
            "side": "yes",
            "type": "limit",
            "yes_price_dollars": "0.01",
            "no_price_dollars": None,
            "initial_count_fp": "1",
            "fill_count_fp": fill_count_fp,
            "remaining_count_fp": str(int(1) - int(fill_count_fp or 0)),
            "taker_fees_dollars": "0.00",
            "maker_fees_dollars": "0.00",
            "taker_fill_cost_dollars": "0.00",
            "maker_fill_cost_dollars": "0.00",
            "user_id": "test-user",
            "client_order_id": client_order_id,
            "created_time": "2026-04-22T00:00:00Z",
            "last_update_time": "2026-04-22T00:00:00Z",
            "expiration_time": None,
        },
    }).encode()


def _markets_response_body() -> bytes:
    return json.dumps({
        "markets": [
            {
                "ticker": "KXBTC-TEST",
                "series_ticker": "KXBTC",
                "event_ticker": "KXBTC-24APR22",
                "status": "open",
                "title": "Test market",
                "subtitle": "",
                "yes_ask": 42,
                "no_ask": 55,
                "volume_24h_fp": "1000",
                "close_time": "2026-05-01T00:00:00Z",
            },
        ],
        "cursor": "",
    }).encode()


def _balance_response_body() -> bytes:
    return json.dumps({
        "balance": 2700,
        "portfolio_value": 2700,
        "updated_ts": 1700000000,
    }).encode()


def _positions_response_body() -> bytes:
    return json.dumps({
        "market_positions": [
            {
                "ticker": "KXBTC-TEST",
                "position_fp": "10",
                "market_exposure_dollars": "0.42",
                "total_traded_dollars": "0.50",
                "resting_orders_count": 0,
                "fees_paid_dollars": "0.00",
                "realized_pnl_dollars": "0.00",
                "last_updated_ts": "2026-04-22T00:00:00Z",
            },
        ],
        "event_positions": [],
    }).encode()


def _exchange_status_body() -> bytes:
    return json.dumps({
        "exchange_active": True,
        "trading_active": True,
    }).encode()


# ----- shared dispatch ----------------------------------------------


def _dispatch(
    request: httpx.Request, *, fill_count_fp: str = "0"
) -> httpx.Response:
    """Route every pykalshi call to a canned response + record the
    outbound body so tests can assert on what we sent."""
    path = request.url.path
    method = request.method
    if method == "POST" and path.endswith("/portfolio/orders"):
        return httpx.Response(
            200,
            content=_order_response_body(fill_count_fp=fill_count_fp),
            headers={"content-type": "application/json"},
        )
    if method == "DELETE" and "/portfolio/orders/" in path:
        return httpx.Response(
            200,
            content=_order_response_body(status="canceled"),
            headers={"content-type": "application/json"},
        )
    if method == "GET" and path.endswith("/markets"):
        return httpx.Response(
            200, content=_markets_response_body(),
            headers={"content-type": "application/json"},
        )
    if method == "GET" and path.endswith("/portfolio/balance"):
        return httpx.Response(
            200, content=_balance_response_body(),
            headers={"content-type": "application/json"},
        )
    if method == "GET" and path.endswith("/portfolio/positions"):
        return httpx.Response(
            200, content=_positions_response_body(),
            headers={"content-type": "application/json"},
        )
    if method == "GET" and "/exchange/status" in path:
        return httpx.Response(
            200, content=_exchange_status_body(),
            headers={"content-type": "application/json"},
        )
    # Anything else -> 404 with debug-friendly body so the test tells
    # us what the probe tried to call.
    return httpx.Response(
        404,
        content=json.dumps({"error": {
            "code": "test_fixture_missing",
            "message": f"no canned response for {method} {path}",
        }}).encode(),
        headers={"content-type": "application/json"},
    )


# ----- real KalshiClient + MockTransport ----------------------------


def _real_sync_client(pem_path: str) -> Any:
    from pykalshi import KalshiClient

    client = KalshiClient(
        api_key_id="probe-test-key-id",
        private_key_path=pem_path,
        demo=True,
    )
    client._session = httpx.Client(transport=httpx.MockTransport(_dispatch))
    return client


def _real_async_client(pem_path: str) -> Any:
    from pykalshi.aclient import AsyncKalshiClient

    client = AsyncKalshiClient(
        api_key_id="probe-test-key-id",
        private_key_path=pem_path,
        demo=True,
    )
    client._session = httpx.AsyncClient(transport=httpx.MockTransport(_dispatch))
    return client


# ========== REGRESSION: the string-where-enum bug ==================


def test_real_pykalshi_rejects_raw_string_action(throwaway_pem):
    """Pin the EXACT failure mode that blew up PR #14 in prod.

    Real pykalshi/_sync/portfolio.py::_build_order_data calls
    `action.value`. Passing `action="buy"` (raw string) crashes with
    `AttributeError: 'str' object has no attribute 'value'`. If a
    future PR removes our enum-conversion and reverts to strings,
    this test catches it in the sandbox."""
    client = _real_sync_client(throwaway_pem)
    with pytest.raises(AttributeError, match="value"):
        client.portfolio.place_order(
            ticker="KXBTC-TEST",
            action="buy",   # wrong -- should be Action.BUY
            side="yes",     # wrong -- should be Side.YES
            count_fp="1",
            yes_price_dollars="0.01",
            client_order_id="test-coid-1",
        )


def test_real_pykalshi_accepts_enum_action(throwaway_pem):
    """The correct path: pass real enum instances. Proves our fix
    works against the real pykalshi code, not a fake."""
    from pykalshi.enums import Action, Side, TimeInForce

    client = _real_sync_client(throwaway_pem)
    order = client.portfolio.place_order(
        ticker="KXBTC-TEST",
        action=Action.BUY,
        side=Side.YES,
        count_fp="1",
        yes_price_dollars="0.01",
        client_order_id="test-coid-2",
        time_in_force=TimeInForce.IOC,
    )
    assert order.order_id == "ord-abc-123"
    # fill_count_fp is a STRING on real OrderModel (the field shape
    # that every earlier PR got wrong).
    assert order.fill_count_fp == "0"
    assert isinstance(order.fill_count_fp, str)


# ========== Probe REST-write-latency against real pykalshi =========


def test_probe_rest_write_against_real_pykalshi(throwaway_pem):
    """End-to-end: RealProbeTransport wraps our RestClient wraps real
    KalshiClient with httpx.MockTransport intercepting. Every layer
    that would have crashed on real prod is exercised here."""
    from kalshi_arb.probe.probe import RealProbeTransport
    from kalshi_arb.rest.client import RestClient, RestConfig

    rest = RestClient(RestConfig(
        api_key_id="probe-test-key-id",
        private_key_path=pathlib.Path(throwaway_pem),
        use_demo=True,
    ))
    # Swap the live httpx sessions for mocked transports.
    rest.underlying._session = httpx.Client(
        transport=httpx.MockTransport(_dispatch)
    )

    transport = RealProbeTransport(rest=rest)
    result = asyncio.run(
        transport.rest_write_latency(
            "KXBTC-TEST", samples=3, coid_tag="write",
        )
    )
    # Every call must have succeeded because our mock returns 200 OK
    # with a real pykalshi-shaped order body. If ANY pykalshi internal
    # raises (string-where-enum, missing field, etc.) the test fails.
    assert result["samples"] == 3, f"got {result}"
    assert result["successful"] == 3, f"got {result}"
    assert "p50_ms" in result
    assert result["errors_summary"]["total_errors"] == 0


def test_probe_rest_write_captures_real_http_rejection(throwaway_pem):
    """When Kalshi returns a real HTTP 403 with a real error body,
    the probe captures the status + parsed code:message. This is the
    case the operator will hit if their account has a real issue."""
    from kalshi_arb.probe.probe import RealProbeTransport
    from kalshi_arb.rest.client import RestClient, RestConfig

    def _reject(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/portfolio/orders"):
            return httpx.Response(
                403,
                content=json.dumps({"error": {
                    "code": "insufficient_buying_power",
                    "message": "balance=$0",
                }}).encode(),
                headers={"content-type": "application/json"},
            )
        return _dispatch(request)

    rest = RestClient(RestConfig(
        api_key_id="probe-test-key-id",
        private_key_path=pathlib.Path(throwaway_pem),
        use_demo=True,
    ))
    rest.underlying._session = httpx.Client(transport=httpx.MockTransport(_reject))

    transport = RealProbeTransport(rest=rest)
    result = asyncio.run(
        transport.rest_write_latency(
            "KXBTC-TEST", samples=3, coid_tag="write",
        )
    )
    assert result["successful"] == 0
    es = result["errors_summary"]
    assert es["total_errors"] == 3
    # Real pykalshi wraps 403 as httpx.HTTPStatusError. Our ErrorCapture
    # extracts .response.status_code.
    sample = es["samples"][0]
    assert sample["http_status"] == 403
    assert "insufficient_buying_power" in sample["body_excerpt"]


# ========== LiveKalshiAPI against real pykalshi =====================


def test_live_api_place_order_against_real_pykalshi(throwaway_pem):
    """LiveKalshiAPI takes our internal OrderRequest (strings for
    action/side/TIF, int cents for price). Must translate every
    field to the real pykalshi type. Exercises the full async
    pykalshi stack."""
    from kalshi_arb.executor.live import LiveKalshiAPI
    from kalshi_arb.executor.kalshi_api import OrderRequest

    os.environ.pop("PAPER_MODE", None)
    api = LiveKalshiAPI.__new__(LiveKalshiAPI)
    api._client = _real_async_client(throwaway_pem)  # type: ignore[attr-defined]

    req = OrderRequest(
        market_ticker="KXBTC-TEST",
        side="yes", action="buy",
        order_type="limit", time_in_force="IOC",
        count=1, limit_cents=1, client_order_id="live-test-coid-1",
    )
    resp = asyncio.run(api.place_order(req))
    # Must not error (the whole point: real pykalshi accepts our args).
    assert resp.error is None, f"place_order errored: {resp.error}"
    assert resp.kalshi_order_id == "ord-abc-123"
    assert resp.filled_count == 0      # fill_count_fp="0" -> 0
    assert resp.fees_cents == 0


def test_live_api_get_portfolio_against_real_pykalshi(throwaway_pem):
    from kalshi_arb.executor.live import LiveKalshiAPI

    os.environ.pop("PAPER_MODE", None)
    api = LiveKalshiAPI.__new__(LiveKalshiAPI)
    api._client = _real_async_client(throwaway_pem)  # type: ignore[attr-defined]

    snap = asyncio.run(api.get_portfolio())
    # Real BalanceModel.balance is int cents. Pre-audit code read
    # `.balance_cents` and got 0 -- this assertion catches that.
    assert snap.cash_cents == 2700
    # Real PositionModel.position_fp is a fixed-point STRING.
    assert snap.positions["KXBTC-TEST"] == 10


# ========== get_orderbook + list_open_markets =======================


def test_rest_client_list_open_markets_against_real_pykalshi(throwaway_pem):
    from kalshi_arb.rest.client import RestClient, RestConfig

    rest = RestClient(RestConfig(
        api_key_id="probe-test-key-id",
        private_key_path=pathlib.Path(throwaway_pem),
        use_demo=True,
    ))
    rest.underlying._session = httpx.Client(
        transport=httpx.MockTransport(_dispatch)
    )
    markets = rest.list_open_markets(limit=10)
    assert len(markets) == 1
    m = markets[0]
    assert m.ticker == "KXBTC-TEST"
    assert m.series_ticker == "KXBTC"


def test_rest_client_list_open_markets_passes_real_enum(throwaway_pem):
    """rest.list_open_markets must pass MarketStatus.OPEN (enum), not
    the string 'open'. pykalshi.get_markets calls .value on status.
    Regression pin for the same trap as action/side."""
    from pykalshi.enums import MarketStatus
    from kalshi_arb.rest.client import RestClient, RestConfig

    captured_paths: list[str] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        captured_paths.append(str(request.url.path) + "?" + str(request.url.query.decode()))
        return _dispatch(request)

    rest = RestClient(RestConfig(
        api_key_id="probe-test-key-id",
        private_key_path=pathlib.Path(throwaway_pem),
        use_demo=True,
    ))
    rest.underlying._session = httpx.Client(transport=httpx.MockTransport(_capture))
    rest.list_open_markets(limit=10)
    # The query string MUST contain status=open (the enum's .value),
    # not status=MarketStatus.OPEN or an error. If our code had passed
    # a raw string "open" instead of MarketStatus.OPEN, pykalshi's
    # `.value` would have crashed BEFORE the request went out.
    joined = " ".join(captured_paths)
    assert "status=open" in joined, f"got: {captured_paths}"
    # Sanity: the enum's .value is the lowercase string.
    assert MarketStatus.OPEN.value == "open"


# ========== Exhaustive smoke: drive every method we call ===========


def test_every_call_site_survives_real_pykalshi(throwaway_pem):
    """Final belt-and-suspenders: walk every call site the probe + the
    live-API exercises, using the real pykalshi stack. If ANY of
    these crashes, we've saved the operator a round-trip."""
    from pykalshi.enums import Action, Side, TimeInForce
    from kalshi_arb.rest.client import RestClient, RestConfig

    rest = RestClient(RestConfig(
        api_key_id="probe-test-key-id",
        private_key_path=pathlib.Path(throwaway_pem),
        use_demo=True,
    ))
    rest.underlying._session = httpx.Client(transport=httpx.MockTransport(_dispatch))

    # 1. list_open_markets (rate limit probe AND universe)
    _ = rest.list_open_markets(limit=5)
    _ = rest.underlying.get_markets(limit=1, fetch_all=False)

    # 2. portfolio.place_order (probe + live)
    order = rest.underlying.portfolio.place_order(
        ticker="KXBTC-TEST",
        action=Action.BUY, side=Side.YES,
        count_fp="1", yes_price_dollars="0.01",
        client_order_id="smoke-1",
        time_in_force=TimeInForce.IOC,
    )
    assert order.order_id

    # 3. portfolio.cancel_order
    cancelled = rest.underlying.portfolio.cancel_order(order_id=order.order_id)
    assert cancelled.order_id

    # (get_orderbook routes through get_market; the market object
    # then calls GET /markets/{ticker}/orderbook which we haven't
    # mocked because WS gap recovery isn't in the probe path. We
    # document that here so the operator knows it's unverified.)
    # UNVERIFIED_WITHOUT_LIVE_PROD: rest.get_orderbook
