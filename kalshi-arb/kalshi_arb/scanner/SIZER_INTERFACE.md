# Scanner → Sizer Interface Spec

**Interface version: 1.0.0** (see `opportunity.py::INTERFACE_VERSION`)

This document defines the exact contract between Module 2 (scanner) and
Module 3 (sizer). Changing any named field below is a breaking change
and must bump `INTERFACE_VERSION`. The test
`test_opportunity_shape_matches_sizer_contract` enforces the field list.

## What the scanner produces

Every `scan()` call returns a `ScanDecision`. The sizer only cares about
decisions where `decision == DECISION_EMIT`; those carry a non-None
`opportunity: Opportunity` payload. Every OTHER decision (skip_halted,
skip_empty, skip_sum_ge_100, skip_below_edge) is still recorded to
`opportunities_detected` by the persistence callback so the ledger is
complete — but they are NOT forwarded to the sizer.

## The `Opportunity` payload

All prices are integer cents (1..99). All sizes are integer contracts.
`net_edge_cents` is a float because fees + slippage can produce fractional
cents after rounding.

| Field | Type | Meaning |
|---|---|---|
| `market_ticker` | str | Full Kalshi ticker, e.g. `KXBTCD-26APR18-T74999.99` |
| `detected_ts_ms` | int | Scanner's detection timestamp (epoch ms UTC) |
| `yes_ask_cents` | int | Best YES ask at detection, cents |
| `yes_ask_qty` | int | Contracts resting at that YES ask |
| `no_ask_cents` | int | Best NO ask at detection, cents |
| `no_ask_qty` | int | Contracts resting at that NO ask |
| `sum_cents` | int | `yes_ask_cents + no_ask_cents` |
| `est_fees_cents` | int | Taker fees for ONE pair of contracts |
| `slippage_buffer_cents` | int | Configured slippage reserve |
| `net_edge_cents` | float | `100 - sum_cents - est_fees_cents - slippage_buffer_cents` |
| `max_liquidity_contracts` | int | `min(yes_ask_qty, no_ask_qty)` |
| `scanner_version` | str | Interface version that produced the opp |

## What the sizer must return

```python
class Sizer(Protocol):
    def size(self, opp: Opportunity, bankroll_cents: int) -> int: ...
```

**Return value**: number of contracts to buy on **each** leg. Both legs
get the same count. Return `0` to skip the trade entirely.

The scanner does NOT enforce a maximum — the sizer is responsible for
capping against `opp.max_liquidity_contracts` and any bankroll/Kelly/hard
caps. The executor will *truncate* any returned count to
`min(returned, opp.max_liquidity_contracts)` as a belt-and-suspenders
guard, but relying on that is a bug.

## What the scanner promises

- **Never emits for a halted market.** `book.is_halted()` is checked
  before any economic analysis.
- **Never emits when either side has zero liquidity at best-ask.**
- **Never emits when `net_edge_cents < config.min_edge_cents`**
  (default 1.0c floor, configurable).
- **Never emits when `sum_cents >= 100`**: no structural edge exists at
  that price.
- **Always records a decision** (emit or skip) to the
  `opportunities_detected` event-store table via the `on_decision`
  callback. The sizer can trust that if it sees an Opportunity, the
  scan is already on disk for audit.

## What the scanner does NOT promise

- It does **not** enforce position sizing caps. Sizer's job.
- It does **not** check the kill switch or daily loss limit. Sizer or
  executor's job.
- It does **not** check bankroll balance. Sizer's job.
- It does **not** check the market's settlement-ambiguity rules (those
  are enforced at universe-selection time via the category whitelist).
- It does **not** deduplicate opportunities across ticks. If the book
  stays in arb-able state for 30 seconds across 100 delta events, the
  scanner emits 100 Opportunity objects. The sizer/executor must use
  client_order_id + ticker to avoid firing duplicate orders.

## Minimum expected profit floor (sizer responsibility)

Per the spec, the sizer must reject trades where
`final_size * net_edge_cents < 50` (i.e. less than 50¢ expected profit).
This rule is NOT enforced by the scanner because the final size is the
sizer's output. The scanner only enforces the per-contract edge floor.

## Migration rules

Bump `INTERFACE_VERSION`:
- **Major** (2.0.0): renaming or removing any field above; changing
  units or meaning of a field.
- **Minor** (1.1.0): adding new fields (additive); adding new decision
  labels.
- **Patch** (1.0.1): internal refactors that preserve the contract.

Every sizer implementation must log `opp.scanner_version` on first
receipt and refuse to process opportunities whose major version doesn't
match its expected major.
