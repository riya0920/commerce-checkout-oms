# Commerce Core: Checkout, Inventory & Order Management

## What it is

The back end of an online store: the part that takes an order, holds stock for
it, charges the card, ships it, and handles returns and refunds.

The hard part is not the happy path. It is what happens when 1,000 people try to
buy the last 50 units at once, when the payment provider times out after taking
the money, or when a customer returns one item from a discounted order. This
project builds each of those cases and checks that no stock is oversold, no
customer is charged twice, and no cent goes missing.

## What we did

1. **Built checkout with stock holds**: two ways to stop overselling, optimistic
   (retry if someone else got there first) and pessimistic (lock the row first).
2. **Handled "payment unknown"**: a reconciliation job asks the payment provider
   what really happened after a timeout, and is safe to run again.
3. **Made the expiry sweeper crash-safe**: releasing held stock survives a crash
   mid-batch.
4. **Kept money in an append-only ledger**: every capture and refund is a row,
   never an edit.
5. **Added per-line tax and partial refunds**, using the same cent-exact
   discount split as the sister project [Promotions & Discount Engine](https://github.com/riya0920/promotions-discount-engine).
6. **Moved hard-coded rules into policy tables**: repricing, return windows,
   restocking fees, exchanges.
7. **Priced stock allocation against a demand forecast** from the sister project
   the [Forecast-to-Price Decision System](https://github.com/riya0920/forecast-to-price-decisions), instead of a simple "days of cover" rule.
8. **Added an HTTP API** with real ownership checks, a time-bucketed funnel, and
   a load generator that measures hidden waiting time.
9. **Re-ran the contention drill on real PostgreSQL**, which showed a failure
   SQLite cannot show.

Work was done in several passes. Later passes re-tested earlier claims, and
several turned out to be wrong.

## Results

**Flash sale (1,000 shoppers, 50 units, SQLite)**

| mechanism | placed | sold out | oversells | p99 ms |
|---|---|---|---|---|
| optimistic | 50 | 950 | **0** | 885.12 |
| pessimistic | 50 | 950 | **0** | 780.15 |

SQLite lets only one writer in at a time, so this is not a fair speed test of the
two mechanisms. It does show neither one oversells.

**Failure drills**

- Payment timed out after capture on 25 of 60 orders. Reconciliation resolved
  all 25. Running it again changed nothing. **0 customers charged twice** (60
  captures at the provider, 60 in our ledger).
- Sweeper crashed 5 times mid-batch: all 200 holds released exactly once.
- 24 identical submits at once: **1 order, 1 charge, 1 unit**.
- Returning a discounted order one item at a time refunds exactly what was
  captured ($78.30 of $78.30).

**Tax and allocation**

- Per-line tax on a sample cart: **$10.28**. One cart-wide rate would have
  charged $11.28, $1.00 more, by taxing exempt groceries.
- Allocation cost over four test scenarios: myopic $1,440.08, days-of-cover rule
  $1,389.52, **forecast $1,346.96**. The forecast policy is priced on one half of
  the samples and graded on the other half.

**Load test**

| | throughput | server p99 | what the user felt, p99 |
|---|---|---|---|
| closed loop | 59.1/s | 213.77 ms | 213.77 ms |
| open loop | 729.9/s | 0.94 ms | **64.13 ms** |

A closed loop stops sending when the server stalls, so it hides the wait. The
open loop keeps its schedule and shows 63 ms of waiting the server never sees.

**PostgreSQL (16 workers, one SKU, plenty of stock)**

| engine | mechanism | granted / attempted | failed with stock on the shelf |
|---|---|---|---|
| sqlite | optimistic | 640 / 640 | 0.0% |
| **postgres** | optimistic | **277 / 640** | **56.7%** |
| postgres | pessimistic | 640 / 640 | 0.0% |

- **0 oversells on every row of both engines.**
- Spreading the load over more SKUs removes the failures (56.7% to 8.1% to 0.0%
  at 1, 4 and 16 SKUs).
- Raising the retry limit from 8 to 64 cuts failures to 2.7% but never to zero.
- Postgres gained more than SQLite from spreading load in 8 of 9 paired runs.

**Bugs found by testing (and fixed)**

- The load generator mixed a fixed schedule with think time and reported a
  1,432 ms p99 against 0.54 ms of real work.
- The first Postgres timer counted connection setup (about 4.8 s) and reported
  452/s against 32/s.
- The first forecast comparison never let the simple rule fire, so it was a straw
  man.
- The HTTP server shared one SQLite connection across threads.
- Repricing kept its own copy of a rule that also lived in the policy table.

## Key decisions and why

**The stock check and the decrement are one SQL statement.**
A loser's update simply changes nothing. There is no gap between "check" and
"take" for another buyer to slip into.

**The payment idempotency key comes from our own attempt id.**
After a timeout, the reconciliation job can search the provider for that key.
Without a key we can look up later, "did the money move?" can never be answered.

**A database UNIQUE constraint decides double-submits.**
Check-then-insert loses the exact race it was written to stop. The loser catches
the error and returns the winner's order, so every caller gets the same answer.

**Order discounts are split onto lines before tax.**
Lines can have different tax rates, so there is no correct single cart rate. The
same split also makes partial refunds exact.

**Merchant rules live in tables, not in code.**
A rule written as a constant is a rule nobody can change or audit. Refusing a
90-day return is a policy outcome the API must be able to say.

**Allocation borrows the shape of the Forecast-to-Price Decision System's forecast, not its demand level.**
These SKUs are not the Forecast-to-Price Decision System's items. Each sampled path is rescaled to the DC's own
demand, so only the uncertainty comes from the Forecast-to-Price Decision System.

**Out of stock is HTTP 409, and someone else's order is 404.**
A 500 tells the client to retry something that cannot work. A 403 would confirm
the order exists.

**Report the shape, not the rate, on Postgres.**
Two runs of the same cell gave 917/s and 563/s. Runs are paired back to back so
machine drift cancels.

## Limits

- **The app still runs on SQLite.** Only the contention drill runs on Postgres.
  Checkout, the sweeper, the ledger and the API were not ported.
- Postgres numbers are a floor: one machine, loopback, fsync off, no connection
  pool.
- No real tax jurisdictions. Rates are per category only.
- Allocation handles one order at a time, with no lookahead over the queue.
- The stockout cost is one constant for every SKU, and results depend on it most.
- Login is a bearer token in a dict. It shows the ownership check, not a real
  security design.
- The storefront has no cart, session or payment form. Buying goes through the
  API.

## How to run

```bash
pip install -r requirements.txt
python run_flashsale.py          # concurrency drills, ~2 min
python run_complete.py           # tax, policies, allocation, load test, ~30 s
uvicorn serve:app --port 8010    # storefront, checkout, ops view

# Postgres drill (needs the local Postgres binaries in .vendor/)
.vendor/pgsql/bin/pg_ctl -D .vendor/pgdata -o "-p 5433" start
python run_postgres.py

python -m pytest tests -q        # 100 tests; the 10 Postgres tests skip if it is not running
```

Full reports: [commerce_report](out/commerce_report.txt) ·
[complete_report](out/complete_report.txt) ·
[postgres_report](out/postgres_report.txt)

## Layout

```
src/checkout.py          checkout steps and their failure handling
src/inventory.py         stock holds, optimistic and pessimistic
src/psp.py               fake payment provider with timeouts
src/lifecycle.py         exchanges, repricing, funnel metrics
src/oms.py               shipments, returns, refunds, ledger
src/taxpolicy.py         per-line tax
src/policies.py          merchant policy tables
src/allocation.py        which DC ships the order
src/forward_position.py  allocation priced against the Forecast-to-Price Decision System's forecast
src/contention.py        contention drill on both engines
src/pgstore.py           the two stock-hold mechanisms on Postgres
src/loadgen.py           open- and closed-loop load generator
serve.py                 HTTP API
```
