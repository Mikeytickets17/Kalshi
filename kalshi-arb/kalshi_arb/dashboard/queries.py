"""Read-only query layer for the dashboard.

One function per tab section. Each takes an EventStore (for read-only
queries) and optional filter kwargs; returns a JSON-serializable dict
the tab template (SSR) and the /{tab}/data endpoint both consume.

Design:
  * Pure reads -- no side effects, no writes. Safe to call from any
    request handler.
  * Dashboard-shaped output: pre-formatted dollar amounts, ISO
    timestamps, trimmed strings. Templates just render.
  * Empty-state discipline: every list is either populated rows or
    the empty list; every tile returns None / 0 / "" when data is
    missing rather than raising. Tabs render a "no data yet" panel
    when the returned list is empty and never a blank table.
  * Time handling: database stores epoch ms; we return both raw ms
    (for charts / CSV) and a precomputed ISO string for readability.

Nothing here imports sqlite3 or any driver. All DB access goes through
the EventStore read_one / read_many primitives.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..store import EventStore


# ---------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------

MS_PER_MIN = 60_000
MS_PER_HOUR = 3_600_000
MS_PER_DAY = 86_400_000


def _now_ms() -> int:
    return int(time.time() * 1000)


def _iso(ms: int | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M:%S")


def _fmt_cents(cents: int | float | None) -> str:
    """Render integer/float cents as signed $x.xx."""
    if cents is None:
        return "—"
    dollars = cents / 100.0
    sign = "-" if dollars < 0 else ""
    return f"{sign}${abs(dollars):,.2f}"


def _start_of_today_ms() -> int:
    now = datetime.now(tz=UTC)
    start = datetime(now.year, now.month, now.day, tzinfo=UTC)
    return int(start.timestamp() * 1000)


def _safe_json(raw: Any) -> Any:
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------

BOT_STATUS_IDLE_THRESHOLD_MS = 5 * MS_PER_MIN


def _latest_kill_switch(store: EventStore) -> dict[str, Any] | None:
    row = store.read_one(
        "SELECT id, last_modified_ms, payload FROM change_log"
        " WHERE entity_type = 'kill_switch' ORDER BY id DESC LIMIT 1"
    )
    if not row:
        return None
    payload = _safe_json(row[2]) or {}
    return {
        "id": row[0],
        "ts_ms": row[1],
        "ts_iso": _iso(row[1]),
        "tripped": bool(payload.get("tripped")),
        "reason": payload.get("reason"),
    }


def _latest_degraded(store: EventStore, within_ms: int = MS_PER_HOUR) -> dict | None:
    cutoff = _now_ms() - within_ms
    row = store.read_one(
        "SELECT id, last_modified_ms, payload FROM change_log"
        " WHERE entity_type = 'degraded' AND last_modified_ms >= ?"
        " ORDER BY id DESC LIMIT 1",
        (cutoff,),
    )
    if not row:
        return None
    payload = _safe_json(row[2]) or {}
    return {
        "id": row[0],
        "ts_ms": row[1],
        "ts_iso": _iso(row[1]),
        "kind": payload.get("kind"),
        "detail": payload.get("detail"),
    }


def _bot_status(store: EventStore) -> str:
    """Compute bot status: KILL-SWITCH > DEGRADED > LIVE > IDLE.

    The rules:
      * KILL-SWITCH: latest kill_switch event has tripped=True.
      * DEGRADED: a 'degraded' entity was logged in the last hour.
      * LIVE: any opportunity was recorded in the last 5 minutes.
      * IDLE: otherwise.
    """
    ks = _latest_kill_switch(store)
    if ks and ks.get("tripped"):
        return "KILL-SWITCH"
    deg = _latest_degraded(store)
    if deg is not None:
        return "DEGRADED"
    cutoff = _now_ms() - BOT_STATUS_IDLE_THRESHOLD_MS
    row = store.read_one(
        "SELECT 1 FROM opportunities_detected WHERE ts_ms >= ? LIMIT 1",
        (cutoff,),
    )
    if row:
        return "LIVE"
    return "IDLE"


def _pnl_realized_total(store: EventStore) -> int:
    row = store.read_one("SELECT COALESCE(SUM(net_cents), 0) FROM pnl_realized")
    return int(row[0]) if row else 0


def _pnl_estimated_total(store: EventStore) -> int:
    """Estimated P&L: sum of net_edge_cents*final_size for emitted
    opportunities that don't yet have a pnl_realized row. This is
    the "theoretical take we're owed" once positions settle."""
    row = store.read_one(
        """
        SELECT COALESCE(SUM(o.net_edge_cents * o.final_size), 0)
        FROM opportunities_detected o
        LEFT JOIN pnl_realized p ON p.opportunity_id = o.id
        WHERE o.decision = 'emit' AND p.id IS NULL
        """
    )
    return int(row[0]) if row else 0


def _opportunities_today_count(store: EventStore) -> int:
    row = store.read_one(
        "SELECT COUNT(*) FROM opportunities_detected WHERE ts_ms >= ?",
        (_start_of_today_ms(),),
    )
    return int(row[0]) if row else 0


def _decisions_per_minute(store: EventStore, hours: int = 6) -> list[dict]:
    """Return 1-minute buckets of opportunities_detected counts for the
    last N hours. Output shape is a list suitable for Chart.js: each
    element has {ts_ms, iso, count}."""
    window_ms = hours * MS_PER_HOUR
    cutoff = _now_ms() - window_ms
    rows = store.read_many(
        """
        SELECT (ts_ms / 60000) * 60000 AS bucket, COUNT(*)
        FROM opportunities_detected
        WHERE ts_ms >= ?
        GROUP BY bucket
        ORDER BY bucket ASC
        """,
        (cutoff,),
    )
    return [
        {"ts_ms": int(r[0]), "iso": _iso(int(r[0])), "count": int(r[1])}
        for r in rows
    ]


def _recent_opportunities(store: EventStore, limit: int = 5) -> list[dict]:
    rows = store.read_many(
        """
        SELECT id, ticker, ts_ms, decision, net_edge_cents, final_size
        FROM opportunities_detected
        ORDER BY id DESC LIMIT ?
        """,
        (limit,),
    )
    return [
        {
            "id": int(r[0]),
            "ticker": r[1],
            "ts_ms": int(r[2]),
            "ts_iso": _iso(int(r[2])),
            "decision": r[3],
            "net_edge_cents": float(r[4] or 0),
            "final_size": int(r[5] or 0),
        }
        for r in rows
    ]


def _recent_executions(store: EventStore, limit: int = 5) -> list[dict]:
    """The 'executions ticker' is derived from orders_placed rows
    (one per leg). We group by opportunity_id so the ticker reads
    one entry per arb, with leg count and YES/NO fill flags."""
    rows = store.read_many(
        """
        SELECT opportunity_id, MAX(placed_ts_ms), ticker,
               COUNT(*) AS legs,
               SUM(CASE WHEN placed_ok = 1 THEN 1 ELSE 0 END) AS ok_legs
        FROM orders_placed
        GROUP BY opportunity_id
        ORDER BY MAX(id) DESC
        LIMIT ?
        """,
        (limit,),
    )
    return [
        {
            "opportunity_id": int(r[0]),
            "ts_ms": int(r[1] or 0),
            "ts_iso": _iso(int(r[1] or 0)),
            "ticker": r[2],
            "legs": int(r[3]),
            "ok_legs": int(r[4] or 0),
        }
        for r in rows
    ]


def overview_data(store: EventStore) -> dict[str, Any]:
    """All Overview-tab data in one call."""
    realized = _pnl_realized_total(store)
    estimated = _pnl_estimated_total(store)
    opps_today = _opportunities_today_count(store)
    return {
        "bot_status": _bot_status(store),
        "kill_switch": _latest_kill_switch(store),
        "tiles": {
            "realized_cents": realized,
            "realized_fmt": _fmt_cents(realized),
            "estimated_cents": estimated,
            "estimated_fmt": _fmt_cents(estimated),
            "opportunities_today": opps_today,
        },
        "decisions_per_minute": _decisions_per_minute(store, hours=6),
        "recent_opportunities": _recent_opportunities(store, limit=5),
        "recent_executions": _recent_executions(store, limit=5),
    }


# ---------------------------------------------------------------------
# Opportunities
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class OpportunityFilters:
    hours: int | None = None        # last N hours; None = all
    ticker: str | None = None        # substring match (case-insensitive)
    decision: str | None = None      # 'emit' | 'skip' | exact decision | None
    min_edge_cents: float | None = None
    limit: int = 500
    offset: int = 0
    sort: str = "ts_ms"              # 'ts_ms' | 'ticker' | 'net_edge_cents'
    sort_dir: str = "desc"           # 'asc' | 'desc'


_OPP_SORT_COLUMNS = {
    "ts_ms": "ts_ms",
    "ticker": "ticker",
    "net_edge_cents": "net_edge_cents",
    "sum_cents": "sum_cents",
    "decision": "decision",
    "final_size": "final_size",
}


def _opp_row_to_dict(r: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "id": int(r[0]),
        "ticker": r[1],
        "ts_ms": int(r[2]),
        "ts_iso": _iso(int(r[2])),
        "yes_ask_cents": int(r[3]),
        "no_ask_cents": int(r[4]),
        "sum_cents": int(r[5]),
        "est_fees_cents": int(r[6]),
        "net_edge_cents": float(r[7] or 0),
        "final_size": int(r[8]),
        "decision": r[9],
        "rejection_reason": r[10],
    }


def opportunities_query(
    store: EventStore, filters: OpportunityFilters | None = None
) -> dict[str, Any]:
    f = filters or OpportunityFilters()
    where: list[str] = []
    params: list[Any] = []
    if f.hours is not None and f.hours > 0:
        where.append("ts_ms >= ?")
        params.append(_now_ms() - f.hours * MS_PER_HOUR)
    if f.ticker:
        where.append("LOWER(ticker) LIKE ?")
        params.append(f"%{f.ticker.lower()}%")
    if f.decision:
        if f.decision == "emit":
            where.append("decision = 'emit'")
        elif f.decision == "skip":
            where.append("decision LIKE 'skip%'")
        else:
            where.append("decision = ?")
            params.append(f.decision)
    if f.min_edge_cents is not None:
        where.append("net_edge_cents >= ?")
        params.append(f.min_edge_cents)
    sort_col = _OPP_SORT_COLUMNS.get(f.sort, "ts_ms")
    sort_dir = "ASC" if f.sort_dir.lower() == "asc" else "DESC"

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    base_sql = (
        "SELECT id, ticker, ts_ms, yes_ask_cents, no_ask_cents, sum_cents,"
        " est_fees_cents, net_edge_cents, final_size, decision, rejection_reason"
        " FROM opportunities_detected"
    )
    limit = max(1, min(f.limit, 5000))
    offset = max(0, f.offset)
    rows = store.read_many(
        f"{base_sql} {where_sql} ORDER BY {sort_col} {sort_dir} LIMIT ? OFFSET ?",
        tuple(params) + (limit, offset),
    )
    count_row = store.read_one(
        f"SELECT COUNT(*) FROM opportunities_detected {where_sql}",
        tuple(params),
    )
    total = int(count_row[0]) if count_row else 0

    return {
        "rows": [_opp_row_to_dict(r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def opportunity_detail(store: EventStore, opp_id: int) -> dict[str, Any] | None:
    """Full detail for the drawer: every column of the opportunity plus
    any orders placed / filled / pnl_realized rows that reference it."""
    row = store.read_one(
        "SELECT id, ticker, ts_ms, yes_ask_cents, yes_ask_qty,"
        " no_ask_cents, no_ask_qty, sum_cents, est_fees_cents,"
        " slippage_buffer, net_edge_cents, max_size_liquidity,"
        " kelly_size, hard_cap_size, final_size, decision, rejection_reason"
        " FROM opportunities_detected WHERE id = ?",
        (opp_id,),
    )
    if not row:
        return None
    opp = {
        "id": int(row[0]),
        "ticker": row[1],
        "ts_ms": int(row[2]),
        "ts_iso": _iso(int(row[2])),
        "book": {
            "yes_ask_cents": int(row[3]),
            "yes_ask_qty": int(row[4]),
            "no_ask_cents": int(row[5]),
            "no_ask_qty": int(row[6]),
            "sum_cents": int(row[7]),
        },
        "fees": {
            "est_fees_cents": int(row[8]),
            "slippage_buffer": int(row[9]),
            "net_edge_cents": float(row[10] or 0),
        },
        "sizer": {
            "max_size_liquidity": int(row[11]),
            "kelly_size": int(row[12]),
            "hard_cap_size": int(row[13]),
            "final_size": int(row[14]),
        },
        "decision": row[15],
        "rejection_reason": row[16],
    }

    orders = store.read_many(
        "SELECT id, client_order_id, kalshi_order_id, side, action, type,"
        " limit_price, count, placed_ts_ms, placed_ok, error"
        " FROM orders_placed WHERE opportunity_id = ? ORDER BY id ASC",
        (opp_id,),
    )
    opp["orders"] = [
        {
            "id": int(o[0]),
            "client_order_id": o[1],
            "kalshi_order_id": o[2],
            "side": o[3],
            "action": o[4],
            "type": o[5],
            "limit_price": int(o[6]),
            "count": int(o[7]),
            "placed_ts_ms": int(o[8]),
            "placed_ts_iso": _iso(int(o[8])),
            "placed_ok": bool(o[9]),
            "error": o[10],
        }
        for o in orders
    ]
    fills_by_coid: dict[str, list[dict]] = {}
    if opp["orders"]:
        coids = [o["client_order_id"] for o in opp["orders"]]
        placeholders = ",".join(["?"] * len(coids))
        fill_rows = store.read_many(
            f"SELECT client_order_id, filled_ts_ms, filled_price, filled_count,"
            f" fees_cents FROM orders_filled WHERE client_order_id IN ({placeholders})"
            f" ORDER BY filled_ts_ms ASC",
            tuple(coids),
        )
        for fr in fill_rows:
            fills_by_coid.setdefault(fr[0], []).append({
                "filled_ts_ms": int(fr[1]),
                "filled_ts_iso": _iso(int(fr[1])),
                "filled_price": int(fr[2]),
                "filled_count": int(fr[3]),
                "fees_cents": int(fr[4] or 0),
            })
    for o in opp["orders"]:
        o["fills"] = fills_by_coid.get(o["client_order_id"], [])

    pnl = store.read_one(
        "SELECT id, settled_ts_ms, yes_pnl_cents, no_pnl_cents, fees_cents,"
        " net_cents, note FROM pnl_realized WHERE opportunity_id = ?"
        " ORDER BY id DESC LIMIT 1",
        (opp_id,),
    )
    opp["pnl_realized"] = (
        None
        if not pnl
        else {
            "id": int(pnl[0]),
            "settled_ts_ms": int(pnl[1]),
            "settled_ts_iso": _iso(int(pnl[1])),
            "yes_pnl_cents": int(pnl[2]),
            "no_pnl_cents": int(pnl[3]),
            "fees_cents": int(pnl[4] or 0),
            "net_cents": int(pnl[5]),
            "net_fmt": _fmt_cents(int(pnl[5])),
            "note": pnl[6],
        }
    )
    return opp


# ---------------------------------------------------------------------
# Trades Taken
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class TradeFilters:
    hours: int | None = None
    ticker: str | None = None
    outcome: str | None = None   # 'open' | 'settled' | 'win' | 'loss' | None
    limit: int = 500
    offset: int = 0
    sort: str = "ts_ms"
    sort_dir: str = "desc"


def _trade_outcome(net_cents: int | None, has_pnl: bool) -> str:
    if not has_pnl:
        return "open"
    if (net_cents or 0) > 0:
        return "win"
    if (net_cents or 0) < 0:
        return "loss"
    return "breakeven"


def trades_query(
    store: EventStore, filters: TradeFilters | None = None
) -> dict[str, Any]:
    """A 'trade' is an opportunity that had at least one order placed.
    We aggregate both legs into a single row: yes/no fill price and
    count, total fees, net P&L (if settled).
    """
    f = filters or TradeFilters()
    where: list[str] = ["EXISTS (SELECT 1 FROM orders_placed op WHERE op.opportunity_id = o.id)"]
    params: list[Any] = []
    if f.hours is not None and f.hours > 0:
        where.append("o.ts_ms >= ?")
        params.append(_now_ms() - f.hours * MS_PER_HOUR)
    if f.ticker:
        where.append("LOWER(o.ticker) LIKE ?")
        params.append(f"%{f.ticker.lower()}%")

    sort_col = {
        "ts_ms": "o.ts_ms",
        "ticker": "o.ticker",
        "net_cents": "p.net_cents",
    }.get(f.sort, "o.ts_ms")
    sort_dir = "ASC" if f.sort_dir.lower() == "asc" else "DESC"

    where_sql = "WHERE " + " AND ".join(where)
    sql = f"""
        SELECT o.id, o.ticker, o.ts_ms, o.final_size, o.net_edge_cents,
               p.net_cents, p.fees_cents, p.settled_ts_ms
        FROM opportunities_detected o
        LEFT JOIN pnl_realized p ON p.opportunity_id = o.id
        {where_sql}
        ORDER BY {sort_col} {sort_dir}
        LIMIT ? OFFSET ?
    """
    limit = max(1, min(f.limit, 5000))
    offset = max(0, f.offset)
    rows = store.read_many(sql, tuple(params) + (limit, offset))

    out: list[dict] = []
    for r in rows:
        opp_id = int(r[0])
        fills = store.read_many(
            """
            SELECT op.side, SUM(f.filled_price * f.filled_count),
                   SUM(f.filled_count), SUM(f.fees_cents)
            FROM orders_placed op
            LEFT JOIN orders_filled f ON f.client_order_id = op.client_order_id
            WHERE op.opportunity_id = ?
            GROUP BY op.side
            """,
            (opp_id,),
        )
        by_side: dict[str, dict] = {}
        for side, price_sum, count_sum, fee_sum in fills:
            count_sum_i = int(count_sum or 0)
            by_side[side] = {
                "count": count_sum_i,
                "avg_price": (
                    round(int(price_sum) / count_sum_i, 2)
                    if count_sum_i > 0
                    else None
                ),
                "fees_cents": int(fee_sum or 0),
            }
        outcome = _trade_outcome(
            int(r[5]) if r[5] is not None else None,
            r[5] is not None,
        )
        if f.outcome and f.outcome != outcome:
            continue
        out.append({
            "opportunity_id": opp_id,
            "ticker": r[1],
            "ts_ms": int(r[2]),
            "ts_iso": _iso(int(r[2])),
            "contracts": int(r[3]),
            "yes": by_side.get("yes", {"count": 0, "avg_price": None, "fees_cents": 0}),
            "no": by_side.get("no", {"count": 0, "avg_price": None, "fees_cents": 0}),
            "outcome": outcome,
            "net_cents": int(r[5]) if r[5] is not None else None,
            "net_fmt": _fmt_cents(int(r[5])) if r[5] is not None else "pending",
            "fees_cents": int(r[6] or 0),
            "settled_ts_ms": int(r[7]) if r[7] is not None else None,
            "settled_ts_iso": _iso(int(r[7])) if r[7] is not None else None,
            "net_edge_cents": float(r[4] or 0),
        })

    count_row = store.read_one(
        f"SELECT COUNT(*) FROM opportunities_detected o {where_sql}",
        tuple(params),
    )
    total = int(count_row[0]) if count_row else 0
    return {"rows": out, "total": total, "limit": limit, "offset": offset}


def trade_detail(store: EventStore, opp_id: int) -> dict[str, Any] | None:
    """Mirrors opportunity_detail but formatted for the Trades drawer:
    both legs side-by-side with fill timeline and unwind annotation."""
    detail = opportunity_detail(store, opp_id)
    if not detail:
        return None
    yes_orders = [o for o in detail["orders"] if o["side"] == "yes"]
    no_orders = [o for o in detail["orders"] if o["side"] == "no"]

    def _summarize_leg(orders: list[dict]) -> dict[str, Any]:
        if not orders:
            return {"orders": [], "fills": [], "total_count": 0,
                    "total_fees_cents": 0, "avg_fill_price": None}
        fills: list[dict] = []
        for o in orders:
            fills.extend(o.get("fills", []))
        total_count = sum(int(fl["filled_count"]) for fl in fills)
        total_price_weight = sum(
            int(fl["filled_price"]) * int(fl["filled_count"]) for fl in fills
        )
        total_fees = sum(int(fl.get("fees_cents", 0) or 0) for fl in fills)
        return {
            "orders": orders,
            "fills": sorted(fills, key=lambda x: x["filled_ts_ms"]),
            "total_count": total_count,
            "total_fees_cents": total_fees,
            "avg_fill_price": (
                round(total_price_weight / total_count, 2)
                if total_count > 0 else None
            ),
        }

    detail["legs"] = {
        "yes": _summarize_leg(yes_orders),
        "no": _summarize_leg(no_orders),
    }
    # Surface an 'unwind' marker if only one leg filled and the other
    # errored out, matching the scanner's critical-unwind criterion.
    yl = detail["legs"]["yes"]
    nl = detail["legs"]["no"]
    detail["unwind"] = {
        "needed": (yl["total_count"] > 0) != (nl["total_count"] > 0),
        "yes_filled": yl["total_count"],
        "no_filled": nl["total_count"],
    }
    return detail


# ---------------------------------------------------------------------
# P&L
# ---------------------------------------------------------------------


def _equity_curve(store: EventStore, hours: int) -> list[dict]:
    cutoff = _now_ms() - hours * MS_PER_HOUR
    rows = store.read_many(
        """
        SELECT settled_ts_ms, net_cents FROM pnl_realized
        WHERE settled_ts_ms >= ? ORDER BY settled_ts_ms ASC
        """,
        (cutoff,),
    )
    running = 0
    out = []
    for ts_ms, net in rows:
        running += int(net)
        out.append({
            "ts_ms": int(ts_ms),
            "iso": _iso(int(ts_ms)),
            "cum_cents": running,
        })
    return out


def _estimated_curve(store: EventStore, hours: int) -> list[dict]:
    cutoff = _now_ms() - hours * MS_PER_HOUR
    rows = store.read_many(
        """
        SELECT ts_ms, net_edge_cents * final_size AS est
        FROM opportunities_detected
        WHERE decision = 'emit' AND ts_ms >= ?
        ORDER BY ts_ms ASC
        """,
        (cutoff,),
    )
    running = 0.0
    out = []
    for ts_ms, est in rows:
        running += float(est or 0)
        out.append({
            "ts_ms": int(ts_ms),
            "iso": _iso(int(ts_ms)),
            "cum_cents": round(running, 2),
        })
    return out


def _daily_bars(store: EventStore, days: int = 30) -> list[dict]:
    cutoff = _now_ms() - days * MS_PER_DAY
    rows = store.read_many(
        """
        SELECT (settled_ts_ms / 86400000) AS day_bucket,
               SUM(net_cents)
        FROM pnl_realized
        WHERE settled_ts_ms >= ?
        GROUP BY day_bucket
        ORDER BY day_bucket ASC
        """,
        (cutoff,),
    )
    return [
        {
            "day_ms": int(r[0]) * MS_PER_DAY,
            "day_iso": _iso(int(r[0]) * MS_PER_DAY),
            "net_cents": int(r[1] or 0),
        }
        for r in rows
    ]


def _category_breakdown(store: EventStore) -> list[dict]:
    rows = store.read_many(
        """
        SELECT COALESCE(m.category, 'uncategorized') AS cat,
               COUNT(*) AS n,
               COALESCE(SUM(p.net_cents), 0) AS net
        FROM pnl_realized p
        JOIN opportunities_detected o ON o.id = p.opportunity_id
        LEFT JOIN markets m ON m.ticker = o.ticker
        GROUP BY cat
        ORDER BY net DESC
        """
    )
    return [
        {"category": r[0], "count": int(r[1]), "net_cents": int(r[2])}
        for r in rows
    ]


def _win_rate_stats(store: EventStore) -> dict[str, Any]:
    row = store.read_one(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN net_cents > 0 THEN 1 ELSE 0 END) AS wins,
            AVG(net_cents) AS avg_captured,
            MIN(net_cents) AS worst,
            MAX(net_cents) AS best
        FROM pnl_realized
        """
    )
    total = int(row[0] or 0) if row else 0
    wins = int(row[1] or 0) if row else 0
    avg_captured = float(row[2] or 0) if row else 0.0
    worst = int(row[3] or 0) if row else 0
    best = int(row[4] or 0) if row else 0

    edge_row = store.read_one(
        """
        SELECT AVG(net_edge_cents) FROM opportunities_detected
        WHERE decision = 'emit'
        """
    )
    avg_detected = float(edge_row[0] or 0) if edge_row and edge_row[0] is not None else 0.0

    # Max drawdown over the full realized curve.
    curve = store.read_many(
        "SELECT settled_ts_ms, net_cents FROM pnl_realized ORDER BY settled_ts_ms ASC"
    )
    peak = 0
    cum = 0
    max_dd = 0
    current_dd = 0
    for _ts, net in curve:
        cum += int(net)
        if cum > peak:
            peak = cum
        drawdown = peak - cum
        if drawdown > max_dd:
            max_dd = drawdown
        current_dd = drawdown

    return {
        "total_trades": total,
        "win_count": wins,
        "win_rate": round(wins / total, 4) if total > 0 else 0.0,
        "avg_edge_captured_cents": round(avg_captured, 2),
        "avg_edge_detected_cents": round(avg_detected, 2),
        "best_cents": best,
        "worst_cents": worst,
        "max_drawdown_cents": max_dd,
        "current_drawdown_cents": current_dd,
    }


def pnl_data(store: EventStore, hours: int = 24 * 7) -> dict[str, Any]:
    return {
        "hours": hours,
        "equity_curve": _equity_curve(store, hours),
        "estimated_curve": _estimated_curve(store, hours),
        "daily_bars": _daily_bars(store, days=30),
        "category_breakdown": _category_breakdown(store),
        "stats": _win_rate_stats(store),
    }


# ---------------------------------------------------------------------
# System Health
# ---------------------------------------------------------------------


def _latest_probe_runs(store: EventStore) -> list[dict]:
    """One row per (env_tag, probe_name): the latest ts_ms, status,
    latency, error. Powers the probe-results table on System Health."""
    rows = store.read_many(
        """
        SELECT p.env_tag, p.probe_name, p.ts_ms, p.status, p.latency_ms, p.error
        FROM probe_runs p
        JOIN (
            SELECT env_tag, probe_name, MAX(ts_ms) AS mx
            FROM probe_runs GROUP BY env_tag, probe_name
        ) m
          ON m.env_tag = p.env_tag
         AND m.probe_name = p.probe_name
         AND m.mx = p.ts_ms
        ORDER BY p.env_tag, p.probe_name
        """
    )
    return [
        {
            "env_tag": r[0],
            "probe_name": r[1],
            "ts_ms": int(r[2]),
            "ts_iso": _iso(int(r[2])),
            "status": r[3],
            "latency_ms": int(r[4]) if r[4] is not None else None,
            "error": r[5],
        }
        for r in rows
    ]


def _ws_pool_status(store: EventStore, minutes: int = 10) -> list[dict]:
    cutoff = _now_ms() - minutes * MS_PER_MIN
    rows = store.read_many(
        """
        SELECT ticker,
               MAX(last_msg_ms) AS last_msg_ms,
               SUM(msg_count) AS msgs,
               SUM(gap_count) AS gaps,
               MAX(last_seq) AS last_seq
        FROM ws_metrics
        WHERE bucket_ts_ms >= ?
        GROUP BY ticker
        ORDER BY last_msg_ms DESC
        """,
        (cutoff,),
    )
    return [
        {
            "ticker": r[0],
            "last_msg_ms": int(r[1] or 0),
            "last_msg_iso": _iso(int(r[1] or 0)),
            "msgs": int(r[2] or 0),
            "gaps": int(r[3] or 0),
            "last_seq": int(r[4]) if r[4] is not None else None,
        }
        for r in rows
    ]


def _recent_degraded(store: EventStore, limit: int = 10) -> list[dict]:
    rows = store.read_many(
        "SELECT id, last_modified_ms, payload FROM change_log"
        " WHERE entity_type = 'degraded' ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    out = []
    for r in rows:
        p = _safe_json(r[2]) or {}
        out.append({
            "id": int(r[0]),
            "ts_ms": int(r[1]),
            "ts_iso": _iso(int(r[1])),
            "kind": p.get("kind"),
            "detail": p.get("detail"),
        })
    return out


def _kill_switch_history(store: EventStore, days: int = 7) -> list[dict]:
    cutoff = _now_ms() - days * MS_PER_DAY
    rows = store.read_many(
        "SELECT id, last_modified_ms, payload FROM change_log"
        " WHERE entity_type = 'kill_switch' AND last_modified_ms >= ?"
        " ORDER BY id DESC",
        (cutoff,),
    )
    out = []
    for r in rows:
        p = _safe_json(r[2]) or {}
        out.append({
            "id": int(r[0]),
            "ts_ms": int(r[1]),
            "ts_iso": _iso(int(r[1])),
            "tripped": bool(p.get("tripped")),
            "reason": p.get("reason"),
        })
    return out


def _unwind_failed_files(logs_dir: Path | None) -> list[dict]:
    """Enumerate CRITICAL_UNWIND_FAILED sentinel files in the configured
    log directory (never raise -- missing dir = empty list)."""
    if logs_dir is None or not logs_dir.exists():
        return []
    out = []
    for path in sorted(logs_dir.glob("CRITICAL_UNWIND_FAILED*")):
        try:
            stat = path.stat()
        except OSError:
            continue
        out.append({
            "name": path.name,
            "path": str(path),
            "size_bytes": stat.st_size,
            "mtime_ms": int(stat.st_mtime * 1000),
            "mtime_iso": _iso(int(stat.st_mtime * 1000)),
        })
    return out


def system_health_data(
    store: EventStore,
    *,
    unwind_logs_dir: Path | None = None,
) -> dict[str, Any]:
    stats = store.stats()
    return {
        "probes": _latest_probe_runs(store),
        "ws_pool": _ws_pool_status(store, minutes=10),
        "event_store": {
            "queue_depth": int(stats.get("queue_depth", 0)),
            "written_total": int(stats.get("written_total", 0)),
            "dropped_total": int(stats.get("dropped_total", 0)),
        },
        "unwind_files": _unwind_failed_files(unwind_logs_dir),
        "degraded_events": _recent_degraded(store, limit=10),
        "kill_switch": _latest_kill_switch(store),
        "kill_switch_history": _kill_switch_history(store, days=7),
        "replica_lag_ms": store.replica_lag_ms(),
    }
