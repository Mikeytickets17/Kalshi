"""CSV streaming for Opportunities + Trades filtered views.

Each exporter reuses the same filter dataclass the JSON endpoint uses,
so the downloaded file and the on-screen table are guaranteed to match.

Uses fastapi.responses.StreamingResponse so very large exports don't
buffer the whole body in memory. csv.writer writes into a StringIO that
we flush per row; the generator yields each flush.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Generator

from fastapi.responses import StreamingResponse

from ..store import EventStore
from .queries import (
    OpportunityFilters,
    TradeFilters,
    opportunities_query,
    trades_query,
)


def _stream_rows(header: list[str], rows: list[list]) -> Generator[str, None, None]:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(header)
    yield buf.getvalue()
    buf.seek(0)
    buf.truncate(0)
    for r in rows:
        writer.writerow(r)
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate(0)


def opportunities_csv(
    store: EventStore, filters: OpportunityFilters | None = None
) -> StreamingResponse:
    """Columns match the on-screen table plus rejection_reason for audit.
    Every field stable for diffing across exports."""
    result = opportunities_query(store, filters or OpportunityFilters(limit=5000))
    header = [
        "id", "ticker", "ts_iso", "ts_ms",
        "yes_ask_cents", "no_ask_cents", "sum_cents",
        "est_fees_cents", "net_edge_cents", "final_size",
        "decision", "rejection_reason",
    ]
    rows = [
        [
            r["id"], r["ticker"], r["ts_iso"], r["ts_ms"],
            r["yes_ask_cents"], r["no_ask_cents"], r["sum_cents"],
            r["est_fees_cents"], r["net_edge_cents"], r["final_size"],
            r["decision"], r["rejection_reason"] or "",
        ]
        for r in result["rows"]
    ]
    return StreamingResponse(
        _stream_rows(header, rows),
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="opportunities.csv"',
            "Cache-Control": "no-store",
        },
    )


def trades_csv(
    store: EventStore, filters: TradeFilters | None = None
) -> StreamingResponse:
    result = trades_query(store, filters or TradeFilters(limit=5000))
    header = [
        "opportunity_id", "ticker", "ts_iso", "ts_ms",
        "contracts",
        "yes_count", "yes_avg_price", "yes_fees_cents",
        "no_count", "no_avg_price", "no_fees_cents",
        "outcome", "net_cents", "net_edge_cents",
        "fees_cents", "settled_ts_iso",
    ]
    rows = []
    for r in result["rows"]:
        y = r["yes"]
        n = r["no"]
        rows.append([
            r["opportunity_id"], r["ticker"], r["ts_iso"], r["ts_ms"],
            r["contracts"],
            y["count"], y["avg_price"] if y["avg_price"] is not None else "",
            y["fees_cents"],
            n["count"], n["avg_price"] if n["avg_price"] is not None else "",
            n["fees_cents"],
            r["outcome"],
            r["net_cents"] if r["net_cents"] is not None else "",
            r["net_edge_cents"],
            r["fees_cents"],
            r["settled_ts_iso"] or "",
        ])
    return StreamingResponse(
        _stream_rows(header, rows),
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="trades.csv"',
            "Cache-Control": "no-store",
        },
    )
