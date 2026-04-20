# Sizer → Executor Interface Spec

**Interface version: 1.0.0** (see `sizer/types.py::INTERFACE_VERSION`)

This document defines the sizer → executor contract plus the executor's
external guarantees. Reviewed and approved 2026-04-17. Any field rename
or semantic change requires a major-version bump and breaks the
interface-shape test.

## Upstream reference

See `scanner/SIZER_INTERFACE.md` for the scanner → sizer contract
(`Opportunity` dataclass + `Sizer` Protocol).

---

## Sizer inputs

The sizer is a **pure, stateless** function. It reads no API, consults
no time, holds no cache. All inputs are passed in.

```python
class Sizer(Protocol):
    def size(self, opp: Opportunity, bankroll: BankrollSnapshot) -> SizingDecision: ...
```

### `BankrollSnapshot`

```python
@dataclass(frozen=True)
class BankrollSnapshot:
    cash_cents: int                     # free-to-trade cash
    open_positions_value_cents: int     # mark-to-market of open positions
    peak_equity_cents: int              # all-time peak (for drawdown-aware Kelly)
    daily_realized_pnl_cents: int       # today's LOCKED-IN P&L only
    taken_at_ms: int                    # read timestamp (UTC epoch ms)
    stale: bool                         # True if read failed, using cached value
```

**Contract**: if `stale` is True, the sizer MUST return `contracts_per_leg=0`.

### `SizingDecision` (the sizer's output)

```python
@dataclass(frozen=True)
class SizingDecision:
    opportunity: Opportunity
    contracts_per_leg: int          # 0 means skip
    reason: str                     # human-readable audit trail
    liquidity_cap: int              # min(yes_qty, no_qty)
    kelly_size: int                 # half-Kelly contracts
    hard_cap_size: int              # HARD_CAP_USD / cost_per_contract
    min_profit_pass: bool           # final_size × edge ≥ MIN_EXPECTED_PROFIT
    bankroll_snapshot: BankrollSnapshot
    sizer_version: str = INTERFACE_VERSION
```

---

## Sizer rules (implemented in `HalfKellySizer`)

Applied in this exact order:

1. **Stale bankroll** → return 0 with reason `"bankroll snapshot stale"`.
2. **Drawdown already past daily limit** → return 0. The caller should
   have already tripped the kill switch; this is belt-and-suspenders.
3. **Liquidity cap**: `cap = min(opp.yes_ask_qty, opp.no_ask_qty)`
4. **Half-Kelly**:
   ```
   cost_per_contract  = opp.yes_ask_cents + opp.no_ask_cents      # cents
   edge_per_contract  = opp.net_edge_cents                         # cents
   kelly_fraction     = 0.5 * edge_per_contract / 100.0            # unit: fraction
   kelly_size         = floor(kelly_fraction * cash_cents / cost_per_contract)
   ```
5. **Hard cap**: `hard_cap_size = floor(HARD_CAP_USD * 100 / cost_per_contract)`
6. **Combined**: `final = max(0, min(cap, kelly_size, hard_cap_size))`
7. **Min-profit floor**: if `final * edge_per_contract < MIN_EXPECTED_PROFIT_USD * 100`,
   set `final = 0` and set `reason` to reflect why.

---

## Executor inputs

```python
class Executor(Protocol):
    async def execute(self, decision: SizingDecision) -> ExecutionResult: ...
```

The executor is stateful: it holds references to the Kalshi API client,
the kill-switch controller, the degraded-mode monitor, and a
daily-realized-P&L counter. It is driven by the sizer's decision object.

### `LegResult`

```python
@dataclass(frozen=True)
class LegResult:
    side: str                      # 'yes' | 'no'
    action: str                    # 'buy' (arb) | 'sell' (unwind)
    limit_cents: int               # 0 for market orders
    requested_count: int
    filled_count: int              # 0 if IOC unfilled
    kalshi_order_id: str | None
    client_order_id: str           # idempotency token (see below)
    placed_ts_ms: int
    first_response_ts_ms: int | None
    error: str | None
```

### `ExecutionResult`

```python
@dataclass(frozen=True)
class ExecutionResult:
    decision: SizingDecision
    fired_ts_ms: int
    legs: tuple[LegResult, ...]    # (yes_leg, no_leg) plus optional unwind legs
    outcome: str                   # see OUTCOME_* constants below
    net_fill_cents: int | None     # signed net cost of everything filled
    total_fees_cents: int
    pnl_confidence: str            # 'realized' | 'estimated_with_unwind' | 'pending_settlement'
    error: str | None
```

`OUTCOME_*` labels (strings):
- `both_filled` — clean arb, both legs filled exactly
- `both_filled_imbalanced_unwound` — both legs had partial fills, imbalance unwound
- `one_filled_unwound` — only one leg filled, filled leg unwound
- `both_rejected` — neither leg filled (IOC timed out)
- `kill_switch` — pre-flight halt, no orders fired
- `halted_by_loss_limit` — daily loss limit trip, no orders fired
- `unwind_failed` — post-fill unwind did not complete within timeout (CRITICAL)

---

## `pnl_confidence` semantics

Per review note C:

| Value | Meaning | Counts toward daily loss limit? |
|---|---|---|
| `realized` | Both arb legs filled cleanly, arb locked in. Settlement outcome doesn't change P&L. | **YES** — count immediately |
| `estimated_with_unwind` | One leg or an imbalance was unwound. Final P&L depends on unwind fill price + any fee refunds at settlement. | **NO** — defer to settlement |
| `pending_settlement` | Open directional position, market not resolved. Used by future modules. | **NO** |

**Hard rule enforced in code**: the daily-P&L counter only sums
`ExecutionResult.net_fill_cents` where `pnl_confidence == 'realized'`.

---

## Idempotency (review note A)

**Every order placed by the executor carries a deterministic
`client_order_id`**:

```python
def client_order_id(
    market_ticker: str,
    detected_ts_ms: int,
    side: str,        # 'yes' | 'no'
    purpose: str = "arb",  # 'arb' | 'unwind'
) -> str:
    raw = f"{market_ticker}|{detected_ts_ms}|{side}|{purpose}"
    return "kac_" + hashlib.sha256(raw.encode()).hexdigest()[:28]
```

Properties enforced by test:
- Same `(ticker, detected_ts_ms, side, purpose)` → **same ID**.
- Any single input changes → **different ID**.
- If `execute()` is called twice with the same `SizingDecision` (e.g.
  crash-retry, operator double-click), Kalshi sees the same
  `client_order_id` on both legs and the second call is deduped
  server-side. Verified by `test_double_execute_same_decision_produces_one_order`.

---

## Unwind policy (review override on Q1)

**Immediate market order. No limit. No 2-second wait.**

Rationale from review: "once we're in the unwind path, we already have
unwanted directional exposure. Slippage is cheaper than holding a naked
leg. Get out fast."

Flow when a leg-imbalance or single-leg fill is detected:

1. Compute the imbalance (review Q4):
   - If both legs partially filled: `unwind_count = |yes_filled - no_filled|` on the over-filled side.
   - If only one leg filled: `unwind_count = filled_count` on that side.
2. Fire a **market BUY-or-SELL** on the over-filled side immediately
   (specifically, SELL the excess YES/NO contracts back to the book at
   market) with `client_order_id = coid(..., purpose="unwind")`.
3. Wait up to 5 seconds for the unwind to fill (review addition B).

### Unwind timeout (review addition B)

If the market-order unwind does not fully fill within 5 seconds:
1. Write `CRITICAL_UNWIND_FAILED_{timestamp}.txt` to the repo root
   containing: timestamp, opportunity dict, all LegResult dicts,
   outstanding contract count, ticker, side.
2. Trip the kill switch with reason `"unwind_failed: {ticker}"`.
3. Log at CRITICAL level.
4. Raise `UnwindFailed` exception. The outer loop turns this into
   `ExecutionResult(outcome='unwind_failed', error=str(exc))`.

Actual pager wiring (SMS/email) is a later push; the sentinel file is
the v1 trigger.

---

## Kill switch

File-based, checked **before every order** and **after every result**.
- **File**: `$KILL_SWITCH_FILE` env var (default `./KILL_SWITCH`).
- **Presence** = halted. Executor returns `outcome='kill_switch'` immediately.
- **Trip triggers**:
  - Manual: operator creates the file.
  - Auto on daily loss limit breach.
  - Auto on degraded-mode detection.
  - Auto on `UnwindFailed`.
- **Reset**: manual only — delete the file.

---

## Degraded-mode detection (review Q2)

Background monitor calls Kalshi's portfolio endpoints on a timer. On
each read, compares to the previous:
- `positions_match` = exact set-and-quantity equality.
- `balance_match` = **exact cent-level match** (no dust tolerance).

Two sequential reads whose positions OR balance differ, **with no
ExecutionResult recorded between them**, triggers
`DegradedModeDetected` which trips the kill switch with reason
`degraded_mode_inconsistent_reads`.

---

## Error types

```python
class ExecutorError(Exception): ...             # base
class KillSwitchTripped(ExecutorError): ...     # pre-flight halt
class UnwindFailed(ExecutorError): ...          # CRITICAL; sentinel file + kill
class DegradedModeDetected(ExecutorError): ...  # sequential reads disagree
class BankrollReadFailed(ExecutorError): ...    # can't read balance/positions
```

---

## Test plan (what's in `tests/`)

### Sizer (`test_sizer.py`)

1. `test_max_size_from_depth` — liquidity cap smallest, wins.
2. `test_half_kelly_hand_calculation` — 5 deterministic inputs vs hand math.
3. `test_hard_cap_wins_over_kelly` — hard cap smaller than kelly.
4. `test_combined_min_rule_runs` — artificially put each of 3 values smallest, verify each wins in turn.
5. `test_min_profit_floor_rejects_low_dollar_trades` — `final×edge < $0.50` → size=0.
6. `test_zero_size_when_bankroll_too_low` — cash smaller than one contract cost.
7. `test_stale_snapshot_rejects_immediately` — size=0 regardless of other inputs.
8. `test_drawdown_already_past_limit_rejects` — daily P&L below floor → size=0.

### Executor (`test_executor.py`)

1. `test_parallel_ioc_dispatch` — `asyncio.gather` places both legs concurrently (wall clock vs mocked per-leg delay).
2. `test_unwind_on_half_fill` — one leg fills 50/50, other 0/50 → unwind 50, `pnl_confidence='estimated_with_unwind'`.
3. `test_unwind_on_imbalance_only` — both legs fill but with imbalance (40/50 vs 45/50) → unwind 5 contracts, `pnl_confidence='estimated_with_unwind'`.
4. `test_unwind_at_market_on_leg_failure` — one leg rejects (not IOC timeout, hard error) → filled leg unwound, `pnl_confidence='estimated_with_unwind'`.
5. `test_unwind_failure_writes_sentinel_and_trips_killswitch` — inject unwind-never-fills; assert sentinel file + kill file + CRITICAL log + `UnwindFailed`.
6. `test_kill_switch_short_circuits` — sentinel exists → no orders fire, `outcome='kill_switch'`.
7. `test_daily_loss_limit_auto_trip` — push realized P&L past threshold, next `execute()` returns `halted_by_loss_limit`.
8. `test_estimated_unwind_does_not_count_toward_daily_limit` — chained unwinds past cash-equivalent-of-limit, limit still NOT tripped because confidence is `estimated_with_unwind`.
9. `test_degraded_mode_trips_kill_switch` — inject two inconsistent reads with no execution between; kill file appears.
10. `test_client_order_id_is_deterministic` — same inputs → same ID; any input changed → different ID.
11. `test_double_execute_same_decision_produces_one_order` — call `executor.execute(decision)` twice; `FakeKalshiAPI` sees one order per leg, not two. Second call returns the same `ExecutionResult` shape.
12. `test_clean_arb_reports_realized_confidence` — both legs filled, no unwind → `pnl_confidence='realized'`.

### Integration (`test_pipeline.py`)

1. `test_scanner_sizer_executor_end_to_end_paper` — synth event stream → scanner emits → sizer decides → paper executor "fires" against `FakeKalshiAPI` → assert full decision trail in the fake event store (one `opportunities_detected` row per scanner decision, one `orders_placed` row per leg, zero real network calls).

---

## What's NOT in Module 3

- Settlement watcher (flips `estimated_with_unwind` → `realized` at market resolution)
- Real pager integration (SMS/email) — sentinel file only for v1
- Backfill of historical fills on restart — out of scope for Module 3 gate
- Dashboard — Module 4
- Backtest — Module 5
