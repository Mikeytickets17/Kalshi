"""Regression tests that pin the real pykalshi API surface.

The operator's 100/100 REST write failures weren't Kalshi rejections.
They were Python `AttributeError: 'KalshiClient' object has no
attribute 'create_order'` from the sandbox having assumed a wrong
API shape. pykalshi's real API is:

    client.portfolio.place_order(
        ticker=..., action=..., side=...,
        count_fp=<str>, yes_price_dollars=<str>,
        client_order_id=..., time_in_force=TimeInForce.IOC,
    ) -> Order

    client.portfolio.cancel_order(order_id=...) -> Order

Both places that call into pykalshi -- the probe + LiveKalshiAPI --
must use THIS shape, not the old `.create_order(..., count=int,
yes_price=int_cents, type='limit')`. These tests exercise each call
site against a deliberately-strict fake that ONLY exposes the real
pykalshi method path + arg names, so any regression back to the
non-existent shape fails instantly.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from kalshi_arb.common.errors import ErrorCapture
from kalshi_arb.executor.kalshi_api import OrderRequest
from kalshi_arb.probe.probe import RealProbeTransport


# ---- Strict pykalshi surface fake ---------------------------------


@dataclass
class _FakePortfolio:
    """Exposes ONLY the real pykalshi portfolio methods. Any attribute
    access outside this whitelist raises AttributeError -- matching
    what happened on the operator's real run."""

    place_order_calls: list[dict[str, Any]] = field(default_factory=list)
    cancel_order_calls: list[dict[str, Any]] = field(default_factory=list)
    place_order_return: Any = None
    place_order_raise: Exception | None = None

    def place_order(self, **kwargs: Any) -> Any:
        self.place_order_calls.append(kwargs)
        if self.place_order_raise is not None:
            raise self.place_order_raise
        return self.place_order_return or _FakeOrder(order_id="fake-1")

    def cancel_order(self, *, order_id: str, **_: Any) -> Any:
        self.cancel_order_calls.append({"order_id": order_id})
        return _FakeOrder(order_id=order_id)


@dataclass
class _FakeOrder:
    order_id: str
    filled_count: int = 0
    status: str = "canceled"


class _FakeKalshiClient:
    """Strict sync client. `.portfolio` only. No top-level create_order.

    Attribute access anywhere else raises -- a regression test will
    catch a future commit that silently brings back the old
    `.create_order(...)` or `.cancel_order(...)` call sites.
    """

    def __init__(self) -> None:
        self.portfolio = _FakePortfolio()

    def __getattr__(self, name: str) -> Any:
        if name in ("portfolio",):
            return super().__getattribute__(name)
        raise AttributeError(
            f"'_FakeKalshiClient' object has no attribute '{name!r}' "
            f"-- the real pykalshi KalshiClient doesn't have it either"
        )


@dataclass
class _FakeRestClient:
    """Matches kalshi_arb.rest.client.RestClient's surface."""

    underlying: _FakeKalshiClient = field(default_factory=_FakeKalshiClient)

    def async_underlying(self) -> Any:
        raise NotImplementedError("not used by these tests")


# ---- Probe: rest_write_latency uses the real pykalshi API ---------


def test_probe_rest_write_uses_portfolio_place_order():
    rest = _FakeRestClient()
    transport = RealProbeTransport(rest=rest)
    asyncio.run(
        transport.rest_write_latency(
            "KXBTC-TEST", samples=3, coid_tag="write",
        )
    )
    # Exactly 3 place_order calls on the portfolio sub-client.
    calls = rest.underlying.portfolio.place_order_calls
    assert len(calls) == 3, f"expected 3, got {len(calls)}"
    # Every call uses pykalshi's real arg names, NOT the old wrong ones.
    for call in calls:
        assert call["ticker"] == "KXBTC-TEST"
        assert call["action"] == "buy"
        assert call["side"] == "yes"
        # count_fp is a STRING, not an int. Regression guard.
        assert call["count_fp"] == "1"
        assert isinstance(call["count_fp"], str)
        # yes_price_dollars is a STRING like "0.01", not integer 1.
        assert call["yes_price_dollars"] == "0.01"
        assert isinstance(call["yes_price_dollars"], str)
        # COID follows the probe- prefix convention.
        assert call["client_order_id"].startswith("probe-write-")
        # time_in_force is pykalshi's enum, not a plain "IOC" string.
        from pykalshi.enums import TimeInForce
        assert call["time_in_force"] is TimeInForce.IOC

    # None of the old wrong kwargs should be present.
    for call in calls:
        assert "type" not in call, "pykalshi doesn't accept 'type'"
        assert "count" not in call, "pykalshi wants count_fp, not count"
        assert "yes_price" not in call, (
            "pykalshi wants yes_price_dollars, not yes_price"
        )


def test_probe_rest_write_cancels_via_portfolio_cancel_order():
    rest = _FakeRestClient()
    rest.underlying.portfolio.place_order_return = _FakeOrder(
        order_id="KX-TEST-123", filled_count=0,
    )
    transport = RealProbeTransport(rest=rest)
    asyncio.run(
        transport.rest_write_latency(
            "KXBTC-TEST", samples=2, coid_tag="write",
        )
    )
    cancels = rest.underlying.portfolio.cancel_order_calls
    # Every place_order that returned an order_id must be cancelled.
    assert len(cancels) == 2
    assert all(c["order_id"] == "KX-TEST-123" for c in cancels)


def test_probe_rest_write_uses_no_longer_exists_create_order_fails():
    """If someone re-introduces .create_order, the fake's __getattr__
    raises AttributeError -- same failure the operator hit on real
    pykalshi. The probe catches it per iteration via ErrorCapture and
    records it; the rest_write result has 0 successful + error samples
    populated with the AttributeError."""
    rest = _FakeRestClient()
    transport = RealProbeTransport(rest=rest)
    result = asyncio.run(
        transport.rest_write_latency(
            "KXBTC-TEST", samples=3, coid_tag="write",
        )
    )
    # Happy path today: all 3 succeed through the fake's place_order.
    assert result["samples"] == 3
    assert result["successful"] == 3
    # Regression: the fake's __getattr__ guard raises on 'create_order',
    # proving that if the probe ever reverts to that call, EVERY
    # sample would fail with 'no attribute create_order' -- exactly
    # the operator's original report.
    with pytest.raises(AttributeError, match="create_order"):
        rest.underlying.create_order(  # type: ignore[attr-defined]
            ticker="x", action="buy", side="yes", count=1, yes_price=1
        )


def test_probe_rest_write_surface_captures_kalshi_http_error():
    """When Kalshi genuinely rejects (not an API-surface bug), the
    probe captures the status + body into errors_summary. This covers
    the original intent of the diagnostic surface work: what the
    operator was MEANT to see once the underlying API-shape bug was
    fixed."""
    rest = _FakeRestClient()

    @dataclass
    class _FakeResp:
        status_code: int
        text: str

    class _HTTPError(Exception):
        def __init__(self) -> None:
            super().__init__("403 Forbidden")
            self.response = _FakeResp(
                status_code=403,
                text='{"error": {"code": "insufficient_buying_power", '
                     '"message": "balance=$0"}}',
            )

    rest.underlying.portfolio.place_order_raise = _HTTPError()
    transport = RealProbeTransport(rest=rest)
    result = asyncio.run(
        transport.rest_write_latency(
            "KXBTC-TEST", samples=3, coid_tag="write",
        )
    )
    assert result["successful"] == 0
    es = result["errors_summary"]
    assert es["total_errors"] == 3
    assert len(es["samples"]) == 1  # one unique group
    s = es["samples"][0]
    assert s["http_status"] == 403
    assert "insufficient_buying_power" in s["body_excerpt"]


# ---- LiveKalshiAPI: place_order + cancel_order use real pykalshi --


def _build_live_api_with_fake():
    """Construct a LiveKalshiAPI instance bypassing __init__'s
    pykalshi handshake so we can swap in a fake async client."""
    import os
    from kalshi_arb.executor.live import LiveKalshiAPI
    os.environ.pop("PAPER_MODE", None)
    api = LiveKalshiAPI.__new__(LiveKalshiAPI)
    api._client = _FakeAsyncKalshi()  # type: ignore[attr-defined]
    return api


@dataclass
class _FakeAsyncPortfolio:
    place_order_calls: list[dict[str, Any]] = field(default_factory=list)
    cancel_order_calls: list[dict[str, Any]] = field(default_factory=list)

    async def place_order(self, **kwargs: Any) -> Any:
        self.place_order_calls.append(kwargs)
        return _FakeOrder(order_id=f"kx-{len(self.place_order_calls)}")

    async def cancel_order(self, *, order_id: str, **_: Any) -> Any:
        self.cancel_order_calls.append({"order_id": order_id})
        return _FakeOrder(order_id=order_id)


class _FakeAsyncKalshi:
    def __init__(self) -> None:
        self.portfolio = _FakeAsyncPortfolio()

    def __getattr__(self, name: str) -> Any:
        if name == "portfolio":
            return super().__getattribute__(name)
        raise AttributeError(
            f"real AsyncKalshiClient doesn't have attribute {name!r}"
        )


def test_live_api_place_order_uses_portfolio_place_order():
    api = _build_live_api_with_fake()
    req = OrderRequest(
        market_ticker="KXBTC-TEST",
        side="yes",
        action="buy",
        order_type="limit",
        time_in_force="IOC",
        count=10,
        limit_cents=42,
        client_order_id="coid-1",
    )
    resp = asyncio.run(api.place_order(req))
    calls = api._client.portfolio.place_order_calls
    assert len(calls) == 1
    call = calls[0]
    assert call["ticker"] == "KXBTC-TEST"
    assert call["action"] == "buy"
    assert call["side"] == "yes"
    # Integer-cent -> dollar-string conversion.
    assert call["yes_price_dollars"] == "0.42"
    # count -> count_fp (string).
    assert call["count_fp"] == "10"
    assert isinstance(call["count_fp"], str)
    # TimeInForce mapped to pykalshi enum.
    from pykalshi.enums import TimeInForce
    assert call["time_in_force"] is TimeInForce.IOC
    # Response kalshi_order_id populated from the Order.order_id.
    assert resp.kalshi_order_id == "kx-1"
    assert resp.error is None


def test_live_api_no_price_path_for_no_side():
    """NO-side orders populate no_price_dollars, not yes_price_dollars."""
    api = _build_live_api_with_fake()
    req = OrderRequest(
        market_ticker="KXBTC-TEST",
        side="no",
        action="buy",
        order_type="limit",
        time_in_force="IOC",
        count=5,
        limit_cents=38,
        client_order_id="coid-no",
    )
    asyncio.run(api.place_order(req))
    call = api._client.portfolio.place_order_calls[-1]
    assert call["no_price_dollars"] == "0.38"
    assert "yes_price_dollars" not in call


def test_live_api_cancel_uses_portfolio_cancel_order():
    api = _build_live_api_with_fake()
    asyncio.run(api.cancel_order("kx-77"))
    cancels = api._client.portfolio.cancel_order_calls
    assert len(cancels) == 1
    assert cancels[0]["order_id"] == "kx-77"


def test_live_api_place_order_returns_error_message_on_pykalshi_failure():
    """If pykalshi raises (e.g. 403 from Kalshi), LiveKalshiAPI's
    wrapper returns an OrderResponse with error=<truncated message>
    rather than propagating. Ensures the executor treats live errors
    the same way as paper errors (no exception crossing the API
    boundary)."""
    api = _build_live_api_with_fake()

    class _Boom:
        async def place_order(self, **_: Any) -> Any:
            raise RuntimeError("insufficient_buying_power: balance=$0")

        async def cancel_order(self, **_: Any) -> Any:
            return _FakeOrder(order_id="x")

    api._client.portfolio = _Boom()  # type: ignore[assignment]
    req = OrderRequest(
        market_ticker="KXBTC-TEST", side="yes", action="buy",
        order_type="limit", time_in_force="IOC",
        count=1, limit_cents=1, client_order_id="coid-fail",
    )
    resp = asyncio.run(api.place_order(req))
    assert resp.error is not None
    assert "insufficient_buying_power" in resp.error
    assert resp.filled_count == 0
