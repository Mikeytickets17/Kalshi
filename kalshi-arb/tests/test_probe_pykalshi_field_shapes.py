"""Pin every pykalshi response-shape field we extract.

Audit of pykalshi's real models (see /usr/local/.../pykalshi/models.py)
revealed three wrong fields baked into probe + LiveKalshiAPI:

    Order.filled_count              -> actually fill_count_fp (str)
    Order.avg_fill_price_cents      -> actually taker_fill_cost_dollars (str)
    Order.fees_cents                -> actually taker_fees_dollars +
                                       maker_fees_dollars (str each)
    Balance.balance_cents           -> actually balance (int)
    Position.position               -> actually position_fp (str)

Plus two top-level KalshiClient methods called that don't exist:

    KalshiClient.get_exchange_status()   -> client.exchange.get_status()
    KalshiClient.get_market_orderbook()  -> client.get_market(t).get_orderbook()

These tests pin the extraction helpers + the dependent call sites so
any regression back to the wrong field names fails instantly.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from kalshi_arb.probe.analysis import (
    fees_cents_from_response,
    fill_count_from_response,
    fill_price_cents_from_response,
    order_id_from_response,
)


# ----- Pydantic-like fake Order matching real pykalshi shape --------


@dataclass
class _FakeOrderModel:
    """Mirrors pykalshi's OrderModel shape: all counts are fixed-point
    STRINGS, not ints; all monetary values are dollar strings."""

    order_id: str = "kx-1"
    ticker: str = "KXBTC-TEST"
    status: str = "resting"
    fill_count_fp: str | None = "0"
    initial_count_fp: str | None = "1"
    remaining_count_fp: str | None = "1"
    yes_price_dollars: str | None = "0.01"
    no_price_dollars: str | None = None
    taker_fill_cost_dollars: str | None = None
    taker_fees_dollars: str | None = None
    maker_fees_dollars: str | None = None


# ----- fill_count_from_response -------------------------------------


def test_fill_count_reads_fill_count_fp_string():
    order = _FakeOrderModel(fill_count_fp="1")
    assert fill_count_from_response(order) == 1


def test_fill_count_reads_fixed_point_decimal_with_zeros():
    assert fill_count_from_response(_FakeOrderModel(fill_count_fp="10.00")) == 10
    assert fill_count_from_response(_FakeOrderModel(fill_count_fp="100")) == 100


def test_fill_count_zero_when_field_is_zero_string():
    assert fill_count_from_response(_FakeOrderModel(fill_count_fp="0")) == 0


def test_fill_count_zero_when_field_missing():
    @dataclass
    class _Empty:
        pass
    assert fill_count_from_response(_Empty()) == 0


def test_fill_count_zero_when_unparseable():
    assert fill_count_from_response(_FakeOrderModel(fill_count_fp="not-a-number")) == 0


def test_fill_count_falls_back_to_legacy_filled_count_int():
    """Tests use plain dicts with the legacy shape. Extractor must
    handle both."""
    assert fill_count_from_response({"filled_count": 5}) == 5


def test_fill_count_fill_count_fp_wins_over_legacy():
    """Real pykalshi shape takes precedence."""
    resp = _FakeOrderModel(fill_count_fp="3")
    resp.filled_count = 99  # type: ignore[attr-defined]
    assert fill_count_from_response(resp) == 3


def test_fill_count_decimal_fractional_truncates_to_int():
    """Kalshi fractional contracts (0.5) exist on a few markets. Our
    probe only fires integer-count orders, but the extractor must
    handle fractional responses without crashing."""
    assert fill_count_from_response(_FakeOrderModel(fill_count_fp="1.5")) == 1


# ----- fill_price_cents_from_response -------------------------------


def test_fill_price_reads_taker_fill_cost_dollars():
    order = _FakeOrderModel(taker_fill_cost_dollars="0.42")
    assert fill_price_cents_from_response(order) == 42


def test_fill_price_handles_round_dollars():
    assert fill_price_cents_from_response(
        _FakeOrderModel(taker_fill_cost_dollars="1.00")
    ) == 100


def test_fill_price_zero_when_missing():
    assert fill_price_cents_from_response(_FakeOrderModel()) == 0


def test_fill_price_falls_back_to_legacy_int():
    assert fill_price_cents_from_response({"avg_fill_price_cents": 55}) == 55


# ----- fees_cents_from_response -------------------------------------


def test_fees_sums_taker_and_maker_dollars():
    order = _FakeOrderModel(
        taker_fees_dollars="0.03",
        maker_fees_dollars="0.01",
    )
    assert fees_cents_from_response(order) == 4


def test_fees_taker_only():
    order = _FakeOrderModel(taker_fees_dollars="0.07")
    assert fees_cents_from_response(order) == 7


def test_fees_zero_when_both_missing():
    assert fees_cents_from_response(_FakeOrderModel()) == 0


def test_fees_legacy_dict_fallback():
    assert fees_cents_from_response({"fees_cents": 3}) == 3


# ----- order_id_from_response ---------------------------------------


def test_order_id_reads_order_id_attr():
    assert order_id_from_response(_FakeOrderModel(order_id="kx-42")) == "kx-42"


def test_order_id_returns_none_when_missing():
    @dataclass
    class _NoId:
        pass
    assert order_id_from_response(_NoId()) is None


def test_order_id_falls_back_to_kalshi_order_id_dict():
    assert order_id_from_response({"kalshi_order_id": "kx-99"}) == "kx-99"


# ----- RestClient uses client.exchange.get_status, not the missing method --


def test_rest_client_server_time_uses_exchange_get_status():
    from kalshi_arb.rest.client import RestClient, RestConfig

    class _FakeExchange:
        def __init__(self) -> None:
            self.called = False

        def get_status(self) -> Any:
            self.called = True

            @dataclass
            class _Status:
                server_time: int = 1700000000000
            return _Status()

    class _FakeSyncClient:
        exchange = _FakeExchange()

        # Attribute guard: any legacy call-through to the
        # non-existent get_exchange_status must blow up immediately.
        def __getattr__(self, name: str) -> Any:
            if name == "exchange":
                return super().__getattribute__(name)
            raise AttributeError(
                f"real KalshiClient has no attribute {name!r}"
            )

    rc = RestClient.__new__(RestClient)
    rc._pyk = _FakeSyncClient()  # type: ignore[attr-defined]
    rc._async_pyk = None  # type: ignore[attr-defined]
    ts = rc.server_time()
    assert ts == 1700000000000
    assert rc._pyk.exchange.called is True  # type: ignore[attr-defined]


def test_rest_client_ping_ms_uses_exchange_get_status():
    from kalshi_arb.rest.client import RestClient

    calls = []

    class _FakeExchange:
        def get_status(self) -> Any:
            calls.append(1)

            @dataclass
            class _Status:
                pass
            return _Status()

    class _FakeSyncClient:
        exchange = _FakeExchange()

    rc = RestClient.__new__(RestClient)
    rc._pyk = _FakeSyncClient()  # type: ignore[attr-defined]
    rc._async_pyk = None  # type: ignore[attr-defined]
    ping = rc.ping_ms()
    assert ping >= 0
    assert len(calls) == 1


def test_rest_client_get_orderbook_uses_market_get_orderbook():
    """Regression: the pre-audit fallback tried a bare GET with a
    path missing its leading slash and an unsupported `params=` kwarg.
    The fix routes through client.get_market(ticker).get_orderbook(...)."""
    from kalshi_arb.rest.client import RestClient

    class _FakeMarket:
        def __init__(self) -> None:
            self.calls = []

        def get_orderbook(self, depth: int | None = None) -> dict:
            self.calls.append(depth)
            return {"yes": [[42, 100]], "no": [[55, 100]]}

    class _FakeSyncClient:
        def __init__(self) -> None:
            self._market = _FakeMarket()

        def get_market(self, ticker: str) -> _FakeMarket:
            return self._market

        # Block any accidental regression to the missing method.
        def __getattr__(self, name: str) -> Any:
            if name in ("get_market",):
                return super().__getattribute__(name)
            if name == "get_market_orderbook":
                raise AttributeError(
                    "real KalshiClient has no get_market_orderbook"
                )
            raise AttributeError(name)

    rc = RestClient.__new__(RestClient)
    rc._pyk = _FakeSyncClient()  # type: ignore[attr-defined]
    rc._async_pyk = None  # type: ignore[attr-defined]
    ob = rc.get_orderbook("KXBTC-TEST", depth=5)
    assert ob == {"yes": [[42, 100]], "no": [[55, 100]]}
    assert rc._pyk._market.calls == [5]  # type: ignore[attr-defined]


# ----- LiveKalshiAPI.get_portfolio reads balance + position_fp ------


def test_live_api_get_portfolio_reads_balance_int_and_position_fp():
    """Pre-audit code read `.balance_cents` (doesn't exist -> 0) and
    `.position` (doesn't exist -> 0). That would have reported an
    empty portfolio on every read + tripped the degraded-mode monitor
    on the first live execution."""
    from kalshi_arb.executor.live import LiveKalshiAPI

    @dataclass
    class _Balance:
        balance: int = 2700  # $27 in cents

    @dataclass
    class _Position:
        ticker: str
        position_fp: str

    class _FakePortfolio:
        async def get_balance(self) -> _Balance:
            return _Balance()

        async def get_positions(self) -> list:
            return [
                _Position(ticker="KXBTC", position_fp="10"),
                _Position(ticker="KXETH", position_fp="-5"),
                _Position(ticker="KXRAIN", position_fp="3.5"),  # fractional
            ]

    class _FakeAsyncClient:
        portfolio = _FakePortfolio()

    api = LiveKalshiAPI.__new__(LiveKalshiAPI)
    api._client = _FakeAsyncClient()  # type: ignore[attr-defined]

    snap = asyncio.run(api.get_portfolio())
    assert snap.cash_cents == 2700
    assert snap.positions["KXBTC"] == 10
    assert snap.positions["KXETH"] == -5
    assert snap.positions["KXRAIN"] == 3   # fractional truncated to int


def test_live_api_place_order_parses_filled_count_from_fp_string():
    """The probe's unfillable-fill guard depends on this parser. If
    Kalshi returns a real fill on what we expected to be a 1c order,
    pykalshi's `fill_count_fp=\"1\"` must translate to filled_count=1
    in the OrderResponse so the executor sees it."""
    from kalshi_arb.executor.live import LiveKalshiAPI
    from kalshi_arb.executor.kalshi_api import OrderRequest

    @dataclass
    class _Order:
        order_id: str = "kx-fill"
        fill_count_fp: str = "1"
        taker_fill_cost_dollars: str = "0.01"
        taker_fees_dollars: str = "0.00"
        maker_fees_dollars: str | None = None

    class _FakePortfolio:
        async def place_order(self, **_: Any) -> _Order:
            return _Order()

        async def cancel_order(self, **_: Any) -> _Order:
            return _Order()

    class _FakeAsyncClient:
        portfolio = _FakePortfolio()

    api = LiveKalshiAPI.__new__(LiveKalshiAPI)
    api._client = _FakeAsyncClient()  # type: ignore[attr-defined]

    req = OrderRequest(
        market_ticker="KXBTC-TEST",
        side="yes", action="buy",
        order_type="limit", time_in_force="IOC",
        count=1, limit_cents=1, client_order_id="probe-test-1",
    )
    resp = asyncio.run(api.place_order(req))
    assert resp.kalshi_order_id == "kx-fill"
    assert resp.filled_count == 1
    assert resp.avg_fill_price_cents == 1
    assert resp.fees_cents == 0
    assert resp.error is None


# ----- WS cap probe no longer claims confirmation when active=0 ----


def test_ws_cap_probe_does_not_confirm_zero_coverage_step():
    """Regression guard: the operator's demo run reported
    max_confirmed_tickers=50 with coverage_pct=0.0 on the first step,
    which was a lie. The fix requires active>0 before the step is
    counted as confirmed."""
    from kalshi_arb.probe.probe import RealProbeTransport

    class _SilentFeed:
        """Pretend every subscribe succeeds but no messages arrive --
        simulating a quiet market window."""

        def __init__(self) -> None:
            self._handlers: dict[str, Any] = {}
            self.subscribe_calls: list[tuple] = []

        def on(self, event: str):
            def deco(fn):
                self._handlers[event] = fn
                return fn
            return deco

        def subscribe(self, channel: str, *, market_tickers) -> None:
            self.subscribe_calls.append((channel, list(market_tickers)))
            # No call to handler -- tickers subscribed but silent.

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *a: Any) -> bool:
            return False

    class _FakeAsync:
        def feed(self) -> Any:
            return _SilentFeed()

    class _FakeRest:
        def async_underlying(self) -> Any:
            return _FakeAsync()

    transport = RealProbeTransport(rest=_FakeRest())
    # 100-ticker pool, step=50 -- expect first step silent, second
    # step drop-off-detected. With fix: max_confirmed_tickers=0.
    tickers = [f"KXBTC-{i:03d}" for i in range(100)]
    # Patch the wait loop to not stall for 10s per step.
    import kalshi_arb.probe.probe as probe_mod
    result = asyncio.run(probe_mod.RealProbeTransport.ws_subscription_cap(
        transport, tickers, step_size=50,
    ))
    assert result["max_confirmed_tickers"] == 0, (
        f"silent-ticker step must not confirm; got "
        f"{result['max_confirmed_tickers']}"
    )
    # drop-off still detected at the second step.
    assert result.get("failure_mode", "").startswith("silent_drop_off")
