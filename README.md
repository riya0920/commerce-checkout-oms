# SE-1 — Commerce Platform: Checkout + Order Management

**Roughly 50% of the spec.** The part after the Buy button, where commerce
software actually fails — plus the four things the first pass named as missing
and has now built: a real allocation engine, exchanges as linked orders, a
repricing policy, and the ops metrics the event log was always for. Still no
storefront, no HTTP API, no real PSP; what remains is named at the bottom.

```bash
python run_flashsale.py     # ~25s  flash sale, 4 drills, allocation, exchanges, ops
python -m pytest tests -q   # 35 tests
```

## Flash sale: 1,000 concurrent checkouts, 50 units

| mechanism | attempted | placed | sold out | **oversell** | errors | throughput | p95 | p99 |
|---|---|---|---|---|---|---|---|---|
| optimistic (CAS) | 1000 | **50** | 950 | **0** | 0 | 669/s | 8.1 ms | 899 ms |
| pessimistic (BEGIN IMMEDIATE) | 1000 | **50** | 950 | **0** | 0 | 529/s | 10.6 ms | 995 ms |

Oversell is impossible because the check and the decrement are **one statement**:

```sql
UPDATE stock SET reserved = reserved + ?, version = version + 1
WHERE sku = ? AND version = ? AND on_hand - reserved >= ?
```

A loser changes nothing and sees `rowcount 0`. **Read the throughput column with
the substitution in mind** — SQLite serialises writers, so this measures the cost
of the optimistic *retry path* under contention, not row-level concurrency. On
Postgres the pessimistic column would be the one paying. Presenting this as
"optimistic wins" would be exactly the overreach the spec screens for.

## Why add-to-cart does not reserve

Cart abandonment runs 65–80%. If add-to-cart reserved stock, every sold unit
would take four-plus units off the shelf for the length of the TTL — a
self-inflicted stockout machine that reports healthy on-hand and can't fulfil.
Reservation starts at **checkout-start**. Ticketmaster reserves at seat-select
because the inventory is unique and the queue *is* the product: same mechanism,
opposite decision, because the scarcity economics differ.

## The four drills

**PSP timed out after capture.** 60 checkouts against a PSP failing 35% of the
time *after* the money moved: 25 landed in `capture_unknown`. The reconciliation
job asks the PSP what happened, searching on an idempotency key derived from our
own attempt id — a capture sent without a key you can look up later makes the
ambiguity permanently unresolvable.

| | |
|---|---|
| examined / confirmed / voided | 25 / 25 / 0 |
| orders left in `capture_unknown` | **0** |
| orders with >1 capture movement | **0** |
| PSP captures vs our ledger | 60 = 60 |
| re-running the job | no-op |

**Sweeper crashed mid-batch.** 200 expired reservations, killed 5 times, then run
to completion → `held=0 released=200 on_hand=500 reserved=0`, no violations. The
guarantee comes from the state guard: the UPDATE that frees stock is predicated
on the row still being `held` and lives in the same transaction that marks it
`released`, so a crash leaves a released prefix and an untouched remainder.

**Double-submit.** 24 concurrent submits of one idempotency key → 1 order, 1
capture, 1 unit. The arbiter is a `UNIQUE` constraint, *not* check-then-insert —
which loses the very race it was written to prevent.

**Reservation expiring mid-payment** (test): the sweeper releases while the PSP
is thinking, and the subsequent commit returns `False` rather than resurrecting
stock already back on the shelf.

## The refund arithmetic

$90.00 order, 20% off, allocated as $6.00 / $3.00 / $9.00. Shipped in two
parcels, delivered, then one item returned:

```
customer returns 1 of 3 — the $45.00 pan
  gross price                 $ 45.00
  less its share of the 20%   -$  9.00
  plus its share of the tax   +$  3.15
  ────────────────────────────────────
  REFUND                      $ 39.15
```

The customer never paid $45.00 for that pan. **Who defined the policy:** the
proportional allocation is set at checkout by SE-2, not invented at return time.
Returning the remaining two brings the ledger to `captured $78.30 / refunded
$78.30 / residual $0.00`, and a property test asserts this for generated
quantities and discount rates — returning every unit as a *separate* event
refunds exactly the capture, because per-unit shares use largest-remainder
apportionment.

Money is **append-only**. An order's financial position is always recomputed from
its movements, so the ledger and the summary cannot drift — there is only one.

## State machine

A table, not if-statements. Tests assert `placed → delivered` raises, terminal
states have no exits, and every transition target is a declared state.

---

# Second pass: the four gaps the first pass named

## Allocation — the decision that used to be a string argument

The first version took `warehouse="DC1"` as an argument to `ship()`. Nothing
decided it, so the "split across warehouses" in drill 5 was a *fixture
describing* a split, not a system *computing* one.

| DC | TEE | MUG | PAN | km to NY |
|---|---|---|---|---|
| DC_NJ | 10 | **0** | 10 | 14 |
| DC_OH | 10 | 10 | **0** | 765 |
| DC_NV | 50 | 50 | 50 | 3,585 |

Order: TEE×2, MUG×1, PAN×1 to New York. **No single DC can fill it.**

| plan | parcels | cost |
|---|---|---|
| naive "nearest DC with everything, else split" → one parcel from Nevada | 1 | $150.58 |
| **allocator** → DC_NJ (TEE×2, PAN×1) + DC_OH (MUG×1) | 2 | **$46.26** |

The extra parcel costs $6.50 in pick-pack-label; the 3,585 km it avoids costs far
more. That is the trade the naive rule cannot see, because *minimise splits* and
*minimise distance* are different objectives and it only has the first.

The answer is a function of the cost constants, not the code —
`PARCEL_COST_CENTS` and `COST_PER_KM_CENTS` are **merchant inputs, not physics**,
named at the top of `src/allocation.py` so a merchant can argue with them rather
than discover them buried in a comparison. Raise the parcel cost far enough and
the single far parcel wins.

**What this is not:** a network optimisation. It scores one order at a time with
no view of the orders behind it, no inventory-position forecast, and no capacity
reservation. Draining the near DC to save a parcel today is a cost you pay
tomorrow, and nothing here models tomorrow.

## Exchanges as linked orders

Refund-then-reorder fails three ways a customer notices: the replacement isn't
reserved so their size sells out mid-flight; they see a debit before the credit
clears and call support about being double-charged; and the price may have moved
so an even swap becomes an argument.

| exchange | returned | replacement | **net** | money movements |
|---|---|---|---|---|
| TEE-M → TEE-L (even) | $32.63 | $32.63 ($30.00 + $2.63 tax) | **$0.00** | **0** |
| TEE-M → TEE-XL (up) | $32.63 | $39.15 ($36.00 + $3.15 tax) | +$6.52 | 1 capture |

**An even exchange moves no money at all.** Refund-then-reorder would produce two
movements for a transaction whose net value is zero, and the customer would watch
their money leave and come back.

*A bug caught by the report contradicting itself:* the first run compared a
tax-**inclusive** refund to a tax-**exclusive** replacement, so a like-for-like
swap quietly netted −$2.63 while the prose claimed zero. The replacement now
carries tax at the same rate, and a test asserts even swaps net exactly zero.

The replacement is **reserved before** the return is processed — if the size is
gone the exchange fails cleanly (`ok=False, replacement_out_of_stock`), the parent
stays `delivered`, and no refund is issued. The customer keeps their item rather
than being refunded into a stockout.

## Repricing between cart and checkout

Policy: absorb rises up to the **smaller** of $3.00 or 5%, always pass decreases
on, re-prompt above the threshold.

| scenario | quoted | current | decision | charged |
|---|---|---|---|---|
| unchanged | $50.00 | $50.00 | unchanged | $50.00 |
| fell to $45 | $50.00 | $45.00 | **reduced** | $45.00 |
| rose $2 | $50.00 | $52.00 | **honoured** | $50.00 |
| rose $12 | $50.00 | $62.00 | **reconfirm** | — |

`reconfirm` is a real outcome, not an error path. Charging more than the customer
agreed to costs a chargeback, and a chargeback costs more than the abandoned cart.
The tolerance being the *smaller* of the two bounds means the percentage binds on
cheap items and the cap on expensive ones — both directions tested. Every
decision is written to `order_events`, so a CS agent asked "why did this cost more
than the email said" has an answer.

## Ops metrics — what the event log was always for

`order_events` existed from the first commit and nothing read it except the
CS-agent audit view. The funnel is a **query over history**, not counters someone
remembered to increment — which is the reason to write transitions to a log
rather than only mutating a state column.

300 checkouts against a PSP declining 8% and timing out 10%, against 220 units:

| step | orders | % of started | step conversion |
|---|---|---|---|
| started | 300 | 100.0% | 100.0% |
| reserved | 144 | 48.0% | 48.0% |
| placed | 124 | 41.3% | 86.1% |
| shipped | 99 | 33.0% | 79.8% |
| delivered | 91 | 30.3% | 91.9% |

On-call health: reservation-expiry 5.6%, payment-failure 2.7%,
**capture_unknown_open 20**.

`capture_unknown_open` is the one to page on. It isn't a rate, it's a *count* of
orders where money may have moved and nobody knows — each is a customer possibly
charged for an order that doesn't exist. It should be driven to zero by the
reconciliation job, and if it isn't, the job is broken.

The 52% out-of-stock rate is high **by construction** (300 checkouts, 220 units).
That's a fixture, not a finding, and the report says so rather than presenting it
as a measurement.

## The other ~50% — what is still NOT here

- **No HTTP API, no storefront, no auth.** Everything is called in-process.
- **SQLite, not Postgres.** This weakens exactly one claim (the concurrency
  benchmark) and the report says so at the point of the claim.
- **Tax is a flat 8.75%.** SE-2 now models per-line rates; SE-1 has not adopted
  them, so the two projects disagree about tax and SE-1 is the wrong one.
- **Shipping is never refunded** on any return — stated as policy in
  `quote_return`, with no configuration and no full-return case that returns it.
- **Allocation is per-order and myopic**: no forward inventory position, no
  capacity reservation, no view of the order queue behind this one.
- **The repricing policy is global**, not per-category or per-customer-tier, and
  there is no re-prompt UI — `reconfirm` is a verdict a caller must act on.
- **Exchanges have no window policy** (30 days, etc) and treat a cheaper
  replacement as a plain net refund.
- **The funnel has no time dimension** — a lifetime aggregate, so it cannot show a
  regression that started on Tuesday, which is most of what a funnel is for.
- **The load test is a thread pool, not a load generator** — no think-time, no
  connection pooling, no separate client process, so p99 includes Python thread
  scheduling and should not be quoted as service latency.

**Linkage:** SE-2's line-item allocation is what makes the proportional refund
computable. `oms.allocate` and `se2/src/money.py::allocate` are the same
algorithm because they have to agree to the cent.
