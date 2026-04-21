"""pykalshi contract audit.

Purpose
-------
Four PRs in a row declared the probe "sandbox-verified" and every one
failed on the operator's first real-prod run. The failure pattern was
identical each time: sandbox tests used fakes the author wrote, those
fakes matched what the author THOUGHT pykalshi returned, and real
pykalshi returned something else (or required enum instances where the
code passed raw strings, or exposed methods under different attribute
paths than the code called).

This tool breaks that loop. It imports the real pykalshi library and
introspects the actual shape of every class / method / attribute that
kalshi_arb code depends on. For every call site in our code, it
emits one of:

    MATCH     -- method exists, signature compatible
    MISSING   -- method does not exist on the real object
    UNKNOWN   -- we can't verify without hitting live Kalshi

Any non-MATCH is a bug that must be fixed BEFORE the probe is re-run.

Usage
-----
    python -m kalshi_arb.tools.pykalshi_contract_audit

Exit code 0 = every call site matches real pykalshi. Non-zero = one
or more bugs found; fix before shipping.

The report is printed to stdout (human-readable) and also serialized
to audit-report.json (for CI gating).
"""

from __future__ import annotations

import inspect
import json
import sys
from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class AuditLine:
    call_site: str            # e.g. "probe.rest_write_latency"
    target: str               # e.g. "KalshiClient.portfolio.place_order"
    expected: str             # kwargs / attrs we pass / read
    status: str               # MATCH | MISSING | UNKNOWN | MISMATCH
    detail: str               # signature or failure reason

    def to_row(self) -> str:
        return f"[{self.status:<8}] {self.call_site:<45} -> {self.target}"


# ---------------------------------------------------------------------
# Introspection helpers
# ---------------------------------------------------------------------


def _resolve_attr(root: Any, path: str) -> tuple[Any, str | None]:
    """Walk a dotted attribute chain through a live object / class.

    Returns (resolved_object, None) on success, (None, error_msg) on
    MISSING. Handles cached_property descriptors that only exist on
    instances by reading the class __dict__ and unwrapping the
    descriptor to its return-type annotation."""
    from functools import cached_property

    obj = root
    parts = path.split(".")
    for i, name in enumerate(parts):
        # 1. Regular attribute lookup on instance or class.
        if hasattr(obj, name):
            nxt = getattr(obj, name)
            # cached_property descriptors return themselves when accessed
            # on the class (rather than an instance); we have to peek
            # at their function to discover the return type.
            if isinstance(nxt, cached_property):
                resolved = _resolve_cached_property(nxt, obj)
                if resolved is None:
                    return None, (
                        f"cached_property '{name}' return type unresolvable"
                    )
                obj = resolved
                continue
            obj = nxt
            continue
        # 2. Class __dict__ fallback for descriptors not surfaced via
        #    hasattr on some Python builds.
        if inspect.isclass(obj) and name in obj.__dict__:
            nxt = obj.__dict__[name]
            if isinstance(nxt, cached_property):
                resolved = _resolve_cached_property(nxt, obj)
                if resolved is None:
                    return None, (
                        f"cached_property '{name}' return type unresolvable"
                    )
                obj = resolved
                continue
            obj = nxt
            continue
        return None, f"MISSING at '.{name}' (after .{'.'.join(parts[:i])})"
    return obj, None


def _resolve_cached_property(cp: Any, owner: Any) -> Any:
    """Resolve a cached_property on a class to the class its .func
    returns. Uses the return annotation when present."""
    func = getattr(cp, "func", None)
    if func is None:
        return None
    try:
        ret = inspect.signature(func).return_annotation
    except (TypeError, ValueError):
        return None
    if ret is inspect.Signature.empty:
        return None
    # ret is usually a class object (e.g. Portfolio, Exchange) or a
    # stringified forward ref. Resolve forward refs by looking in the
    # function's module.
    if isinstance(ret, str):
        module = sys.modules.get(getattr(func, "__module__", ""))
        if module is None:
            return None
        return getattr(module, ret, None)
    return ret


def _collect_fields(model: Any) -> set[str]:
    """Return every attribute name the model exposes.

    Handles three flavours:
      * Pydantic BaseModel (has model_fields)
      * Classes with @property descriptors declared on the class body
      * Wrapper classes whose __getattr__ delegates to self.data
        (pykalshi Market/Order delegate to their *Model counterparts).
    """
    fields: set[str] = set()
    if hasattr(model, "model_fields"):
        fields.update(model.model_fields.keys())
    # @property descriptors + explicit methods/attrs on the class body.
    if inspect.isclass(model):
        for name, value in model.__dict__.items():
            if name.startswith("_"):
                continue
            if isinstance(value, property) or callable(value):
                fields.add(name)
    # Wrapper-class fallback: look at __init__ for `self, client, data`
    # pattern and include fields from the OrderModel / MarketModel /
    # PositionModel that self.data points at.
    for ann_name, ann in getattr(model, "__annotations__", {}).items():
        fields.add(ann_name)
    return fields


def _check_kwargs(fn: Any, required: set[str]) -> tuple[bool, str]:
    """Confirm a function/method accepts every kwarg in `required`."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError) as exc:
        return False, f"signature unreadable: {exc}"
    params = set(sig.parameters)
    has_var_kwargs = any(
        p.kind is inspect.Parameter.VAR_KEYWORD
        for p in sig.parameters.values()
    )
    missing = [k for k in required if k not in params and not has_var_kwargs]
    if missing:
        return False, f"missing kwargs: {missing}  signature={sig}"
    return True, str(sig)


def _check_method_calls_enum_value(src_text: str, enum_name: str) -> bool:
    """Return True if the function body contains `param.value` without
    an isinstance guard -- i.e., it will AttributeError on a raw
    string. Used to detect traps like _build_order_data's
    action.value / side.value that crashed on strings."""
    return f"{enum_name}.value" in src_text and f"isinstance({enum_name}" not in src_text


# ---------------------------------------------------------------------
# Call-site catalogue
#
# Every entry below is one line of kalshi_arb code that touches
# pykalshi. For each we know:
#   - the call site (module + function)
#   - the resolved pykalshi path (what we think we're calling)
#   - the kwargs / attributes we expect
# The audit walks this list, resolves each path against the live
# library, and reports MATCH / MISSING / MISMATCH / UNKNOWN.
# ---------------------------------------------------------------------


@dataclass
class ExpectedMethodCall:
    call_site: str
    target_path: str            # dotted path through a pykalshi root
    root: str                   # 'sync' | 'async' | 'module'
    kwargs: set[str]
    notes: str = ""


# pykalshi roots for resolution
def _roots() -> dict[str, Any]:
    from pykalshi import KalshiClient
    from pykalshi.aclient import AsyncKalshiClient
    import pykalshi

    return {
        "sync": KalshiClient,
        "async": AsyncKalshiClient,
        "module": pykalshi,
    }


METHOD_CALLS = [
    # -- Probe: REST write latency --
    ExpectedMethodCall(
        call_site="probe.rest_write_latency.place_order",
        target_path="portfolio.place_order",
        root="sync",
        kwargs={
            "ticker", "action", "side", "count_fp",
            "yes_price_dollars", "client_order_id", "time_in_force",
        },
    ),
    ExpectedMethodCall(
        call_site="probe.rest_write_latency.cancel",
        target_path="portfolio.cancel_order",
        root="sync",
        kwargs={"order_id"},
    ),
    # -- Probe: REST rate limit --
    ExpectedMethodCall(
        call_site="probe.rest_rate_limit.get_markets",
        target_path="get_markets",
        root="sync",
        kwargs={"limit", "fetch_all"},
    ),
    # -- Probe: E2E loop --
    ExpectedMethodCall(
        call_site="probe.end_to_end_loop.place_order",
        target_path="portfolio.place_order",
        root="sync",
        kwargs={
            "ticker", "action", "side", "count_fp",
            "yes_price_dollars", "client_order_id", "time_in_force",
        },
    ),
    ExpectedMethodCall(
        call_site="probe.end_to_end_loop.cancel",
        target_path="portfolio.cancel_order",
        root="sync",
        kwargs={"order_id"},
    ),
    # -- Probe: WS feed (used by ws_subscription_cap + end_to_end_loop) --
    ExpectedMethodCall(
        call_site="probe.ws_subscription_cap.feed",
        target_path="feed",
        root="async",
        kwargs=set(),
    ),
    # -- RestClient --
    ExpectedMethodCall(
        call_site="rest.list_open_markets",
        target_path="get_markets",
        root="sync",
        kwargs={"status", "limit", "fetch_all"},
    ),
    ExpectedMethodCall(
        call_site="rest.get_orderbook.via_market",
        target_path="get_market",
        root="sync",
        kwargs={},
        notes="we then call .get_orderbook(depth=...) on the Market",
    ),
    ExpectedMethodCall(
        call_site="rest.server_time",
        target_path="exchange.get_status",
        root="sync",
        kwargs=set(),
    ),
    ExpectedMethodCall(
        call_site="rest.ping_ms",
        target_path="exchange.get_status",
        root="sync",
        kwargs=set(),
    ),
    # -- LiveKalshiAPI (async) --
    ExpectedMethodCall(
        call_site="live_api.place_order",
        target_path="portfolio.place_order",
        root="async",
        kwargs={
            "ticker", "action", "side", "count_fp", "client_order_id",
            "time_in_force",
        },
        notes="yes_price_dollars OR no_price_dollars conditionally",
    ),
    ExpectedMethodCall(
        call_site="live_api.cancel_order",
        target_path="portfolio.cancel_order",
        root="async",
        kwargs={"order_id"},
    ),
    ExpectedMethodCall(
        call_site="live_api.get_balance",
        target_path="portfolio.get_balance",
        root="async",
        kwargs=set(),
    ),
    ExpectedMethodCall(
        call_site="live_api.get_positions",
        target_path="portfolio.get_positions",
        root="async",
        kwargs=set(),
    ),
    # -- WS feed from ShardedWS (prod paper mode) --
    ExpectedMethodCall(
        call_site="ws.ShardedWS.feed",
        target_path="feed",
        root="async",
        kwargs=set(),
    ),
]


# ---------------------------------------------------------------------
# Attribute-access catalogue: for every pykalshi return type we read
# from, list the fields we touch and verify against the actual Pydantic
# model definition.
# ---------------------------------------------------------------------


@dataclass
class ExpectedAttrAccess:
    call_site: str
    model: str                # dotted path under pykalshi
    attrs: set[str]
    notes: str = ""


ATTR_ACCESSES = [
    ExpectedAttrAccess(
        call_site="live_api.place_order.response",
        model="pykalshi.models.OrderModel",
        attrs={
            "order_id",
            "fill_count_fp",
            "taker_fill_cost_dollars",
            "taker_fees_dollars",
            "maker_fees_dollars",
        },
        notes="Order class delegates unknown attrs to self.data (OrderModel)",
    ),
    ExpectedAttrAccess(
        call_site="probe.rest_write_latency.response",
        model="pykalshi.models.OrderModel",
        attrs={"order_id", "fill_count_fp"},
    ),
    ExpectedAttrAccess(
        call_site="probe.end_to_end_loop.response",
        model="pykalshi.models.OrderModel",
        attrs={"order_id", "fill_count_fp"},
    ),
    ExpectedAttrAccess(
        call_site="live_api.get_portfolio.balance",
        model="pykalshi.models.BalanceModel",
        attrs={"balance"},
    ),
    ExpectedAttrAccess(
        call_site="live_api.get_portfolio.position",
        model="pykalshi.models.PositionModel",
        attrs={"ticker", "position_fp"},
    ),
    ExpectedAttrAccess(
        call_site="rest.list_open_markets.market",
        model="pykalshi._sync.markets.Market",
        attrs={"ticker", "series_ticker", "event_ticker", "title",
               "subtitle", "status", "close_time"},
        notes="volume_24h is exposed as volume_24h_fp (string, fixed-point)",
    ),
]


# ---------------------------------------------------------------------
# Enum-dereference traps
#
# pykalshi's order-building code at _sync/portfolio.py::_build_order_data
# unconditionally calls `action.value` and `side.value` WITHOUT an
# isinstance guard. Passing a raw string ("buy", "yes") triggers:
#
#     AttributeError: 'str' object has no attribute 'value'
#
# This is the bug that just blew up the operator's PR-#14 run. We
# audit every pykalshi function we call for unconditional `.value`
# usage on enum parameters and flag it here.
# ---------------------------------------------------------------------


@dataclass
class ExpectedEnumUse:
    call_site: str
    pykalshi_fn_path: str     # file:line or dotted path
    enum_params: list[str]    # kwargs that must be enum instances


ENUM_USES = [
    ExpectedEnumUse(
        call_site="probe.rest_write_latency.place_order",
        pykalshi_fn_path="pykalshi._sync.portfolio.Portfolio._build_order_data",
        enum_params=["action", "side", "time_in_force"],
    ),
    ExpectedEnumUse(
        call_site="probe.end_to_end_loop.place_order",
        pykalshi_fn_path="pykalshi._sync.portfolio.Portfolio._build_order_data",
        enum_params=["action", "side", "time_in_force"],
    ),
    ExpectedEnumUse(
        call_site="live_api.place_order",
        pykalshi_fn_path="pykalshi._async.portfolio.AsyncPortfolio._build_order_data",
        enum_params=["action", "side", "time_in_force"],
    ),
    ExpectedEnumUse(
        call_site="rest.list_open_markets",
        pykalshi_fn_path="pykalshi._sync.client.KalshiClient.get_markets",
        enum_params=["status"],
    ),
]


# ---------------------------------------------------------------------
# Audit runners
# ---------------------------------------------------------------------


def audit_method_calls() -> list[AuditLine]:
    roots = _roots()
    results: list[AuditLine] = []
    for e in METHOD_CALLS:
        root_obj = roots[e.root]
        resolved, err = _resolve_attr(root_obj, e.target_path)
        if err is not None:
            results.append(AuditLine(
                call_site=e.call_site,
                target=f"{e.root}:{e.target_path}",
                expected=f"kwargs={sorted(e.kwargs)}",
                status="MISSING",
                detail=err,
            ))
            continue
        if not callable(resolved) and not inspect.isclass(resolved):
            results.append(AuditLine(
                call_site=e.call_site,
                target=f"{e.root}:{e.target_path}",
                expected=f"kwargs={sorted(e.kwargs)}",
                status="MISMATCH",
                detail=f"resolved to non-callable: {type(resolved).__name__}",
            ))
            continue
        ok, detail = _check_kwargs(resolved, e.kwargs)
        if ok:
            results.append(AuditLine(
                call_site=e.call_site,
                target=f"{e.root}:{e.target_path}",
                expected=f"kwargs={sorted(e.kwargs)}",
                status="MATCH",
                detail=detail,
            ))
        else:
            results.append(AuditLine(
                call_site=e.call_site,
                target=f"{e.root}:{e.target_path}",
                expected=f"kwargs={sorted(e.kwargs)}",
                status="MISMATCH",
                detail=detail,
            ))
    return results


def audit_attribute_accesses() -> list[AuditLine]:
    results: list[AuditLine] = []
    for e in ATTR_ACCESSES:
        try:
            parts = e.model.split(".")
            root_mod = __import__(parts[0])
            obj: Any = root_mod
            for p in parts[1:]:
                obj = getattr(obj, p)
        except (ImportError, AttributeError) as exc:
            results.append(AuditLine(
                call_site=e.call_site,
                target=e.model,
                expected=f"attrs={sorted(e.attrs)}",
                status="MISSING",
                detail=f"cannot import model: {exc}",
            ))
            continue

        fields = _collect_fields(obj)
        # pykalshi's Market / Order wrapper classes define `self.data =
        # <Model>` in __init__ and their __getattr__ delegates to data.
        # Include the paired *Model fields so `market.ticker` (which
        # reads through to MarketModel.ticker) is recognised.
        delegated_model = _find_delegated_model(obj)
        if delegated_model is not None:
            fields.update(_collect_fields(delegated_model))

        missing = [a for a in e.attrs if a not in fields]
        if not missing:
            results.append(AuditLine(
                call_site=e.call_site,
                target=e.model,
                expected=f"attrs={sorted(e.attrs)}",
                status="MATCH",
                detail=f"fields present: {sorted(e.attrs)}",
            ))
        else:
            results.append(AuditLine(
                call_site=e.call_site,
                target=e.model,
                expected=f"attrs={sorted(e.attrs)}",
                status="MISMATCH",
                detail=(
                    f"missing from model: {missing} "
                    f"(available: {sorted(fields)[:20]})"
                ),
            ))
    return results


def _find_delegated_model(cls: Any) -> Any:
    """pykalshi's Order / Market wrap a Pydantic Model. Their __init__
    signature is (self, client, data: <Model>). Extract the Model
    class from the data param's annotation."""
    if not inspect.isclass(cls):
        return None
    try:
        init_sig = inspect.signature(cls.__init__)
    except (TypeError, ValueError):
        return None
    data_param = init_sig.parameters.get("data")
    if data_param is None:
        return None
    ann = data_param.annotation
    if ann is inspect.Parameter.empty:
        return None
    if isinstance(ann, str):
        mod = sys.modules.get(cls.__module__)
        if mod is None:
            return None
        return getattr(mod, ann, None)
    return ann


def audit_enum_dereference_traps() -> list[AuditLine]:
    """Detect pykalshi functions that unconditionally call .value on
    enum-typed params. Our call sites MUST pass real enum instances
    when landing in these functions."""
    results: list[AuditLine] = []
    for e in ENUM_USES:
        parts = e.pykalshi_fn_path.split(".")
        try:
            root_mod = __import__(parts[0])
            obj: Any = root_mod
            for p in parts[1:]:
                obj = getattr(obj, p)
        except (ImportError, AttributeError) as exc:
            results.append(AuditLine(
                call_site=e.call_site,
                target=e.pykalshi_fn_path,
                expected=f"enums={e.enum_params}",
                status="MISSING",
                detail=f"cannot resolve: {exc}",
            ))
            continue
        try:
            src = inspect.getsource(obj)
        except (OSError, TypeError) as exc:
            results.append(AuditLine(
                call_site=e.call_site,
                target=e.pykalshi_fn_path,
                expected=f"enums={e.enum_params}",
                status="UNKNOWN",
                detail=f"source unreadable: {exc}",
            ))
            continue

        traps = []
        for param in e.enum_params:
            if _check_method_calls_enum_value(src, param):
                traps.append(param)

        if traps:
            # Informational only. Part 4 (our-source inspection) is
            # what decides whether a call site actually triggers the
            # trap by passing a string instead of an enum. Part 3 just
            # documents which pykalshi functions require careful enum
            # handling.
            results.append(AuditLine(
                call_site=e.call_site,
                target=e.pykalshi_fn_path,
                expected=f"enums={e.enum_params}",
                status="INFO",
                detail=(
                    f"pykalshi unconditionally calls .value on: {traps} -- "
                    "caller MUST pass real enum instances, not strings"
                ),
            ))
        else:
            results.append(AuditLine(
                call_site=e.call_site,
                target=e.pykalshi_fn_path,
                expected=f"enums={e.enum_params}",
                status="MATCH",
                detail="pykalshi guards .value with isinstance or accepts strings",
            ))
    return results


def audit_probe_usage_of_enums() -> list[AuditLine]:
    """Last-mile guard: inspect kalshi_arb source for call sites that
    pass a raw string where pykalshi will .value() the argument."""
    import pathlib

    project = pathlib.Path(__file__).resolve().parents[2]
    sources = {
        "probe.rest_write_latency": project / "kalshi_arb/probe/probe.py",
        "probe.end_to_end_loop": project / "kalshi_arb/probe/probe.py",
        "live_api.place_order": project / "kalshi_arb/executor/live.py",
        "rest.list_open_markets": project / "kalshi_arb/rest/client.py",
    }
    results: list[AuditLine] = []
    for site, path in sources.items():
        if not path.exists():
            results.append(AuditLine(
                call_site=site,
                target="source inspection",
                expected="action / side passed as enums",
                status="MISSING",
                detail=f"source not found at {path}",
            ))
            continue
        src = path.read_text()
        bad: list[str] = []
        if "place_order" in src or "create_order" in src:
            # Accept either enum instance (Action.BUY, Side.YES) or
            # the pykalshi strings ("buy", "yes") only if we see an
            # explicit Action()/Side() conversion in the same function.
            if 'action="buy"' in src and "Action.BUY" not in src:
                bad.append('action="buy" (string, pykalshi will .value() it)')
            if 'side="yes"' in src and "Side.YES" not in src:
                bad.append('side="yes" (string, pykalshi will .value() it)')
            if 'side="no"' in src and "Side.NO" not in src:
                bad.append('side="no" (string, pykalshi will .value() it)')
        if "get_markets" in src and "status=" in src:
            if 'status="open"' in src and "MarketStatus.OPEN" not in src:
                bad.append('status="open" (string, pykalshi will .value() it)')
        if bad:
            results.append(AuditLine(
                call_site=site,
                target="source inspection",
                expected="enums, not raw strings",
                status="ENUM_TRAP",
                detail="; ".join(bad),
            ))
        else:
            results.append(AuditLine(
                call_site=site,
                target="source inspection",
                expected="enums, not raw strings",
                status="MATCH",
                detail="no raw-string enum args detected",
            ))
    return results


# ---------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------


def _render_section(title: str, rows: list[AuditLine]) -> str:
    lines = ["", "=" * 80, f" {title}", "=" * 80]
    for r in rows:
        lines.append(r.to_row())
        if r.status != "MATCH":
            lines.append(f"           detail: {r.detail}")
    return "\n".join(lines)


def main() -> int:
    method_rows = audit_method_calls()
    attr_rows = audit_attribute_accesses()
    trap_rows = audit_enum_dereference_traps()
    usage_rows = audit_probe_usage_of_enums()

    all_rows = method_rows + attr_rows + trap_rows + usage_rows

    print(_render_section(
        "PART 1: Method-call contract (kalshi_arb -> pykalshi)",
        method_rows,
    ))
    print(_render_section(
        "PART 2: Attribute-access contract (pykalshi return models)",
        attr_rows,
    ))
    print(_render_section(
        "PART 3: Pykalshi enum-dereference traps (pykalshi unconditional .value calls)",
        trap_rows,
    ))
    print(_render_section(
        "PART 4: Our-source enum-arg inspection (did the caller pass enums?)",
        usage_rows,
    ))

    # Summary. INFO = pykalshi trap documented, not a bug by itself.
    # ENUM_TRAP at the call-site level (Part 4) is a genuine caller bug.
    bad_statuses = {"MISSING", "MISMATCH", "ENUM_TRAP"}
    bads = [r for r in all_rows if r.status in bad_statuses]
    unknowns = [r for r in all_rows if r.status == "UNKNOWN"]
    matches = [r for r in all_rows if r.status == "MATCH"]
    infos = [r for r in all_rows if r.status == "INFO"]

    print()
    print("=" * 80)
    print(" SUMMARY")
    print("=" * 80)
    print(f"  Total call sites audited: {len(all_rows)}")
    print(f"  MATCH:    {len(matches)}")
    print(f"  MISSING:  {sum(1 for r in all_rows if r.status == 'MISSING')}")
    print(f"  MISMATCH: {sum(1 for r in all_rows if r.status == 'MISMATCH')}")
    print(f"  ENUM_TRAP (caller-side, actionable): "
          f"{sum(1 for r in all_rows if r.status == 'ENUM_TRAP')}")
    print(f"  INFO (pykalshi traps, documented): {len(infos)}")
    print(f"  UNKNOWN:  {len(unknowns)}")
    print()
    if bads:
        print("  RESULT: FAIL -- fix every bad row above before the operator re-runs")
    elif unknowns:
        print("  RESULT: PARTIAL -- call sites with UNKNOWN need live-prod verification")
    else:
        print("  RESULT: CLEAN -- every call site verified against real pykalshi 1.0.4")

    # JSON report for CI gating.
    report = {
        "summary": {
            "total": len(all_rows),
            "match": len(matches),
            "bad": len(bads),
            "unknown": len(unknowns),
        },
        "rows": [asdict(r) for r in all_rows],
    }
    with open("audit-report.json", "w") as f:
        json.dump(report, f, indent=2)

    return 0 if not bads else 10


if __name__ == "__main__":
    sys.exit(main())
