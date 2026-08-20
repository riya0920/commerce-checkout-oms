# SE-1 — Commerce Platform: Checkout + Order Management

**This is not deployable.** It is the first ~20% of the spec: the part after the
Buy button, which is where commerce software actually fails. No storefront, no
HTTP API, no real PSP. The missing 80% is named at the bottom.

```bash
python run_flashsale.py     # ~10s  flash sale + 4 failure drills
python -m pytest tests -q   # 18 tests
```

## Flash sale: 1,000 concurrent checkouts, 50 units

| mechanism | attempted | placed | sold out | **oversell** | errors | throughput | p95 | p99 |
|---|---|---|---|---|---|---|---|---|
| optimistic (CAS) | 1000 | **50** | 950 | **0** | 0 | 669/s | 8.1 ms | 899 ms |
| pessimistic (BEGIN IMMEDIATE) | 1000 | **50** | 950 | **0** | 0 | 529/s | 10.6 ms | 995 ms |

Ending stock `on_hand=0 reserved=0`, 50 committed reservations, zero invariant
violations, zero deadlocks, on both.

Oversell is impossible because the check and the decrement are **one statement**:

```sql
UPDATE stock SET reserved = reserved + ?, version = version + 1
WHERE sku = ? AND version = ? AND on_hand - reserved >= ?
```

A loser changes nothing and sees `rowcount 0`. There is no window in which two
callers both believe they hold the last unit.

**Read the throughput column with the substitution in mind.** SQLite serialises
writers at the database level, so this is *not* a measurement of optimistic vs
pessimistic row-level concurrency — both are queueing behind one global write
lock. What it does show is the cost of the optimistic **retry path** under
contention: every loser re-reads and re-issues, and on a 50-unit sale with 1,000
shoppers almost everyone loses. On Postgres with row locks the pessimistic column
would be the one paying. Presenting this as "optimistic wins" would be exactly
the overreach the spec screens for — the benchmark exists, and it needs a real
engine before it decides anything.

## Why add-to-cart does not reserve

Cart abandonment runs 65–80%. If add-to-cart reserved stock, every sold unit
would take four-plus units off the shelf for the length of the TTL — a
self-inflicted stockout machine that reports healthy on-hand and can't fulfil.
Reservation starts at **checkout-start**, where intent is demonstrated and the
population is small enough that a 10-minute hold is cheap.

Ticketmaster reserves at seat-select because the inventory is unique,
non-substitutable, and the queue *is* the product. Same mechanism, opposite
decision, because the scarcity economics differ.

## The four drills

**PSP timed out after capture.** 60 checkouts against a PSP failing 35% of the
time *after* the money moved: 25 landed in `capture_unknown`. The reconciliation
job asks the PSP what actually happened (searching on an idempotency key derived
from our own attempt id — a capture sent without a key you can look up later
makes the ambiguity permanently unresolvable):

| | |
|---|---|
| examined / confirmed / voided | 25 / 25 / 0 |
| orders left in `capture_unknown` | **0** |
| orders with >1 capture movement | **0** |
| captures settled at PSP vs in our ledger | 60 = 60 |
| re-running the job | no-op, as required |

**Sweeper crashed mid-batch.** 200 expired reservations, sweeper killed 5 times
partway through, then run to completion → `held=0 released=200 on_hand=500
reserved=0`, no invariant violations. The guarantee comes from the state guard,
not from bookkeeping: the UPDATE that frees stock is predicated on the row still
being `held` and lives in the same transaction that marks it `released`, so a
crash leaves a released prefix and an untouched remainder. No interleaving
returns stock twice or leaks it.

**Double-submit.** 24 concurrent submits of one idempotency key → 1 order,
1 capture, 1 unit consumed. The arbiter is a `UNIQUE` constraint, *not* a
check-then-insert — which loses the very race it was written to prevent. The
loser catches `IntegrityError` and reads back the winner's order.

**Reservation expiring mid-payment** (test only): the sweeper releases while the
PSP is thinking, and the subsequent commit returns `False` rather than
resurrecting stock that is already back on the shelf.

## The refund arithmetic

This is the case the spec says it will check. $90.00 order, 20% off, allocated to
lines as $6.00 / $3.00 / $9.00. Shipped in two parcels, delivered, then one item
returned:

```
customer returns 1 of 3 — the $45.00 pan
  gross price                 $ 45.00
  less its share of the 20%   -$  9.00
  plus its share of the tax   +$  3.15
  ────────────────────────────────────
  REFUND                      $ 39.15
```

The customer never paid $45.00 for that pan. Refunding the ticket price hands
back $9.00 of discount that was never charged. **Who defined the policy:** the
proportional allocation is set at checkout by the promotions engine (SE-2), not
invented at return time — which is why SE-2's allocation invariant is exact to
the cent.

Returning the remaining two brings the ledger to `captured $78.30 / refunded
$78.30 / residual $0.00`. A property test asserts this for generated quantities
and discount rates: returning every unit as a *separate* return event refunds
exactly the capture. Per-unit discount and tax shares use largest-remainder
apportionment, so no cent is created on the first return or stranded on the last
— the failure a naive per-unit division produces, which surfaces months later as
a penny accounting cannot explain.

Money is **append-only**. Nothing updates or deletes a `money_movements` row; an
order's financial position is always recomputed from its movements, so the ledger
and the summary cannot drift apart — there is only one of them.

## State machine

A table, not if-statements. `transition()` refuses anything not in it, so illegal
transitions are unreachable rather than merely uncalled. Tests assert that
`placed → delivered` and `placed → returned` raise, that terminal states have no
exits, and that every transition target is a declared state.

`pending → reserved → placed → allocated → picked → packed → partially_shipped →
shipped → delivered → return_requested → partially_returned → returned`, plus the
failure branches `capture_unknown`, `payment_failed`, `abandoned_out_of_stock`,
`cancelled`.

## The other 80% — what is NOT here

- **No HTTP API, no storefront, no auth.** Everything is called in-process.
- **SQLite, not Postgres.** This weakens exactly one claim (the concurrency
  benchmark above) and the report says so at the point of the claim.
- **Tax is a flat 8.75%.** Real tax is a nexus and jurisdiction problem, rates
  differ per line, and an order-level discount allocated across lines with
  *different* rates is its own correctness problem. None of that is modelled.
- **No repricing policy.** The spec asks what happens when the price changes
  between cart and checkout; this build prices once at checkout and never
  revalidates.
- **No exchanges.** The spec asks for exchanges as linked orders; there is no
  link field and no flow.
- **Shipping is never refunded** on any return, partial or full — stated as
  policy in `quote_return`, but with no configuration and no full-return case
  that returns it.
- **No allocation engine.** `allocated` is a state a caller sets; nothing decides
  *which* warehouse serves which line, so the "split across warehouses" in the
  drill is asserted by the test, not computed.
- **No metrics or dashboards** — no checkout conversion by step, no
  reservation-expiry rate, no capture-unknown counter as an operational signal.
  The `order_events` table is the raw material for all three and nothing reads it
  except the CS-agent audit view.
- **The load test is a thread pool, not a load generator** — no think-time, no
  connection pooling, no separate client process, so the p99 numbers include
  Python thread scheduling and should not be quoted as service latency.

**Linkage:** SE-2's line-item allocation is what makes the proportional refund
above computable. That is the pairing the spec describes, and it is real here —
`oms.allocate` and `se2/src/money.py::allocate` are the same algorithm because
they have to agree to the cent.
