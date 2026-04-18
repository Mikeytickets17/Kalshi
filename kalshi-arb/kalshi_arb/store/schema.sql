-- Event store schema. All timestamps are epoch milliseconds UTC.
-- Prices are stored as integer cents (1..99). Sizes are integer contracts.
--
-- Compatible with both SQLite (local dev + bot on laptop) and libSQL
-- (Turso primary + embedded replica on Fly). No dialect-specific SQL.
-- SQLite-only PRAGMAs are no-ops under libSQL.

PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;
PRAGMA temp_store = MEMORY;

-- Known markets. Row inserted on first sighting via REST or WS.
CREATE TABLE IF NOT EXISTS markets (
    ticker         TEXT PRIMARY KEY,
    series_ticker  TEXT NOT NULL,
    event_ticker   TEXT,
    title          TEXT,
    subtitle       TEXT,
    category       TEXT,               -- crypto/weather/econ/other
    status         TEXT NOT NULL,      -- open/paused/closed/settled/...
    open_ts_ms     INTEGER,
    close_ts_ms    INTEGER,
    first_seen_ms  INTEGER NOT NULL,
    last_seen_ms   INTEGER NOT NULL,
    excluded       INTEGER NOT NULL DEFAULT 0,  -- 1 if ambiguous settlement
    excluded_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_markets_series ON markets(series_ticker);
CREATE INDEX IF NOT EXISTS idx_markets_status ON markets(status);
CREATE INDEX IF NOT EXISTS idx_markets_category ON markets(category);

-- Raw orderbook_delta messages. Event-sourced — the book at any point in
-- time can be reconstructed by replaying deltas since the most recent snapshot.
CREATE TABLE IF NOT EXISTS orderbook_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker     TEXT NOT NULL,
    ts_ms      INTEGER NOT NULL,
    seq        INTEGER NOT NULL,       -- from Kalshi; monotonic per market
    side       TEXT NOT NULL CHECK (side IN ('yes', 'no')),
    price      INTEGER NOT NULL,       -- cents, 1..99
    delta      INTEGER NOT NULL,       -- contracts added (+) or removed (-)
    event_kind TEXT NOT NULL           -- 'delta' | 'snapshot' | 'gap_resnap'
);
CREATE INDEX IF NOT EXISTS idx_ob_ticker_ts ON orderbook_events(ticker, ts_ms);
CREATE INDEX IF NOT EXISTS idx_ob_ticker_seq ON orderbook_events(ticker, seq);

-- Periodic full snapshots every 10 minutes. Bounds replay cost.
CREATE TABLE IF NOT EXISTS orderbook_snapshots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker     TEXT NOT NULL,
    ts_ms      INTEGER NOT NULL,
    seq        INTEGER NOT NULL,
    yes_levels BLOB NOT NULL,          -- msgpack: [[price, size], ...]
    no_levels  BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snap_ticker_ts ON orderbook_snapshots(ticker, ts_ms);

-- Trade tape.
CREATE TABLE IF NOT EXISTS trades (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker     TEXT NOT NULL,
    ts_ms      INTEGER NOT NULL,
    price      INTEGER NOT NULL,
    count      INTEGER NOT NULL,
    taker_side TEXT NOT NULL CHECK (taker_side IN ('yes', 'no'))
);
CREATE INDEX IF NOT EXISTS idx_trades_ticker_ts ON trades(ticker, ts_ms);

-- Every opportunity the scanner evaluates — traded or rejected.
-- This table is the core audit trail for the whole system.
CREATE TABLE IF NOT EXISTS opportunities_detected (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker             TEXT NOT NULL,
    ts_ms              INTEGER NOT NULL,
    yes_ask_cents      INTEGER NOT NULL,
    yes_ask_qty        INTEGER NOT NULL,
    no_ask_cents       INTEGER NOT NULL,
    no_ask_qty         INTEGER NOT NULL,
    sum_cents          INTEGER NOT NULL,
    est_fees_cents     INTEGER NOT NULL,
    slippage_buffer    INTEGER NOT NULL,
    net_edge_cents     REAL NOT NULL,
    max_size_liquidity INTEGER NOT NULL,
    kelly_size         INTEGER NOT NULL,
    hard_cap_size      INTEGER NOT NULL,
    final_size         INTEGER NOT NULL,
    decision           TEXT NOT NULL,  -- traded/skip_below_edge/skip_below_profit/skip_halted/skip_excluded/kill_switch
    rejection_reason   TEXT
);
CREATE INDEX IF NOT EXISTS idx_opp_ts ON opportunities_detected(ts_ms);
CREATE INDEX IF NOT EXISTS idx_opp_decision ON opportunities_detected(decision);

-- Orders placed (both legs of an arb share an opportunity_id).
CREATE TABLE IF NOT EXISTS orders_placed (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id  TEXT UNIQUE NOT NULL,
    kalshi_order_id  TEXT,
    opportunity_id   INTEGER NOT NULL,
    ticker           TEXT NOT NULL,
    side             TEXT NOT NULL CHECK (side IN ('yes', 'no')),
    action           TEXT NOT NULL CHECK (action IN ('buy', 'sell')),
    type             TEXT NOT NULL,
    limit_price      INTEGER NOT NULL,
    count            INTEGER NOT NULL,
    placed_ts_ms     INTEGER NOT NULL,
    placed_ok        INTEGER NOT NULL DEFAULT 0,
    error            TEXT,
    FOREIGN KEY (opportunity_id) REFERENCES opportunities_detected(id)
);
CREATE INDEX IF NOT EXISTS idx_orders_opp ON orders_placed(opportunity_id);

-- Fills (a single order may have multiple fills).
CREATE TABLE IF NOT EXISTS orders_filled (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id TEXT NOT NULL,
    filled_ts_ms    INTEGER NOT NULL,
    filled_price    INTEGER NOT NULL,
    filled_count    INTEGER NOT NULL,
    fees_cents      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_fills_coid ON orders_filled(client_order_id);

-- Realized P&L, computed on settlement or on close-out.
CREATE TABLE IF NOT EXISTS pnl_realized (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_id INTEGER NOT NULL,
    settled_ts_ms  INTEGER NOT NULL,
    yes_pnl_cents  INTEGER NOT NULL,
    no_pnl_cents   INTEGER NOT NULL,
    fees_cents     INTEGER NOT NULL,
    net_cents      INTEGER NOT NULL,
    note           TEXT
);
CREATE INDEX IF NOT EXISTS idx_pnl_opp ON pnl_realized(opportunity_id);

-- Observability: WS message counts + gap events.
CREATE TABLE IF NOT EXISTS ws_metrics (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    bucket_ts_ms INTEGER NOT NULL,
    ticker       TEXT NOT NULL,
    msg_count    INTEGER NOT NULL,
    gap_count    INTEGER NOT NULL DEFAULT 0,
    last_seq     INTEGER,
    last_msg_ms  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ws_bucket ON ws_metrics(bucket_ts_ms);
CREATE INDEX IF NOT EXISTS idx_ws_ticker ON ws_metrics(ticker);

-- Schema version marker. Bump when migrating.
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('version', '2');

-- ---------------------------------------------------------------------
-- Change capture (dashboard polling primitive).
--
-- Every domain-level write goes through an EventStore helper which
-- stamps last_modified_ms = clock.now_ms() AND inserts a row here.
-- The dashboard polls:
--   SELECT entity_type, entity_id, ts_ms
--     FROM change_log
--    WHERE id > :last_seen_id
--    ORDER BY id ASC;
-- and fans each row out via SSE to connected browsers.
--
-- Works identically on SQLite and libSQL. On a Turso embedded replica,
-- the rows appear here after the next sync() pull -- so the dashboard's
-- replica lag is bounded by the sync_interval_sec config.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS change_log (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type      TEXT NOT NULL,      -- 'opportunity' | 'execution' | 'kill_switch' | ...
    entity_id        INTEGER,            -- optional FK to the entity's own id
    last_modified_ms INTEGER NOT NULL,   -- epoch ms UTC
    payload          TEXT                -- optional JSON blob for small events
);
CREATE INDEX IF NOT EXISTS idx_change_log_ts ON change_log(last_modified_ms);
CREATE INDEX IF NOT EXISTS idx_change_log_entity ON change_log(entity_type, last_modified_ms);
