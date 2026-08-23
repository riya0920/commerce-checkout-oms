# SE-1 — Commerce Core: Checkout, Inventory & Order Management

**Complete against the spec.** Zero oversells at a 1,000-concurrent flash sale by
two mechanisms, capture-unknown reconciliation, a crash-safe sweeper, an
append-only money ledger, **per-line tax adopted from SE-2**, policy tables where
constants used to be, **forward-looking allocation**, a **time-bucketed funnel**,
an HTTP surface with real authorisation, and a load generator that measures
coordinated omission.

```bash
python run_flashsale.py      # ~2min  the concurrency drills
python run_complete.py       # ~30s   tax, policies, allocation, the load test
uvicorn serve:app --port 8010   #      storefront, checkout, ops view
python -m pytest tests -q    # 76 tests
```

## Two of my own projects disagreed about a number

The previous README said it plainly: *"SE-2 now models per-line rates; SE-1 has
not adopted them, so the two projects disagree about tax and SE-1 is the wrong
one."* Two services in one portfolio computed a different tax on the same cart,
and the one that was right had no way to make the one that was wrong agree.

| line | gross | discount | taxable | rate bp | tax |
|---|---|---|---|---|---|
| TEE-BLUE | $49.98 | −$8.00 | $41.98 | 875 | $3.67 |
| MILK | $13.47 | −$2.16 | $11.31 | **0** | **$0.00** |
| HEADPHONES | $89.99 | −$14.40 | $75.59 | 875 | $6.61 |
| | | | | | **$10.28** |

A single cart rate would have charged **$11.28 — $1.00 more, by taxing the exempt
groceries.** There is no correct single cart rate when the lines differ, which is
exactly why the order-level discount has to be *allocated to lines* before tax is
computable at all. That is the same allocation SE-2's promo engine and this
project's partial refunds both stand on.

**Manufacturer vs retailer discount**, on the same line: retailer-funded taxes
$41.98 and manufacturer-funded taxes $49.98 — **$0.70 of tax on one line**. A
retailer discount reduces the taxable receipt; a manufacturer coupon does not,
because the retailer is reimbursed. It is a per-line flag rather than prose,
because it changes the number and somebody will eventually ask.

## Policies that were constants

Every one of these was a hard-coded literal standing where a **merchant decision**
belongs — which is the actual defect, and a more common one than any individual
missing feature. A policy expressed as a constant in a function is a policy nobody
can change, argue with, or audit.

### Repricing, per category and per tier

| category | tier | quoted | current | decision | charged |
|---|---|---|---|---|---|
| apparel | standard | $50.00 | $52.00 | honoured | $50.00 |
| electronics | standard | $1200.00 | $1220.00 | **reconfirm** | — |
| electronics | gold | $1200.00 | $1220.00 | **honoured** | $1200.00 |
| apparel | standard | $50.00 | $45.00 | reduced | $45.00 |

The same **+$20** rise is honoured for apparel and reconfirmed for electronics,
because the tolerance is the *smaller* of a cash cap and a percentage — so the
percentage binds on expensive items and the cap on cheap ones.

The tier column is a real and slightly uncomfortable decision: a programme that
absorbs more for high-value customers **is** price discrimination by tenure. It is
legal and widespread, and it belongs in a table somebody signed off rather than in
an if-statement somebody wrote.

> `lifecycle.reprice` now **calls** this table instead of keeping its own copy of
> the tolerance. It had one — two implementations of one rule in one codebase,
> which is precisely the defect the tax section above is about.

### Returns: shipping, windows, restocking

| category | days | full return | shipping back | restocking | refund |
|---|---|---|---|---|---|
| apparel | 10 | yes | **$7.95** | $0.00 | $57.95 |
| apparel | 10 | no | $0.00 | $0.00 | $50.00 |
| apparel | 90 | yes | — | — | **REFUSED** |
| electronics | 3 | yes | $7.95 | $0.00 | $57.95 |
| electronics | 20 | yes | $7.95 | **−$7.50** | $50.45 |

**Shipping comes back on a full return and not on a partial one**, and the reason
is not arbitrary: a partial return still required the parcel, so the shipping was
consumed. A full return means the shipment should not have happened.

The 90-day row is **refused**, and refusing is a policy outcome the API has to be
able to express. The previous version had no window at all — which is not a
generous policy, it is an absent one.

### Exchanges, including the case that was wrong

| case | returned → replacement | movement |
|---|---|---|
| even swap | $32.63 → $32.63 | **none** |
| upgrade | $32.63 → $39.15 | capture $6.52 |
| cheaper, above floor | $39.15 → $32.63 | refund $6.52 |
| cheaper, **below floor** | $32.63 → $32.00 | **store credit $0.63** |

A cheaper replacement is not simply a net refund. Below a floor the refund costs
more to process than it returns — payment fees, a statement line the customer
queries, a support contact — so small differences become store credit. The floor
is a merchant input, and stating it is the point: the alternative is a silent
rounding customers notice and support cannot explain.

## Allocation that can see tomorrow

The near DC has 12 units and **3.0 days of cover**. Where does the far DC start to
win?

| far DC km | near ship | far ship | near scarcity penalty | near total | winner |
|---|---|---|---|---|---|
| 100 | $7.06 | $10.50 | $5.40 | $12.46 | **FAR** |
| 300 | $7.06 | $18.50 | $5.40 | $12.46 | near |
| 3,585 | $7.06 | $149.90 | $5.40 | $12.46 | near |

A myopic scorer picks the near DC on **every** row, because shipping from 14 km is
cheaper than shipping from anywhere. Adding the cost of draining a DC with three
days of cover flips the decision once the far DC is within 100 km.

**The flip point is the useful output, not the winner.** It says how much scarcity
is worth in shipping-distance terms — a number a network planner can argue with —
and it is entirely a function of two merchant constants ($6.50 per parcel, $0.04
per km).

> The first version of this section compared the near DC against one 3,585 km
> away, where the shipping difference is $143 and no plausible penalty could ever
> flip it. It demonstrated a mechanism that could not matter.

## A load test that is actually a load test

| | throughput | service p99 | perceived p99 | omission gap |
|---|---|---|---|---|
| closed loop (40 ms think) | 59.1/s | 213.77 ms | 213.77 ms | **0.00** |
| open loop (6 ms arrivals) | **729.9/s** | 0.94 ms | **64.13 ms** | **63.19** |

**The closed loop's omission gap is zero by construction, and that is the problem
with it rather than a good property.** A request is "due" when the client becomes
free, so a client blocked on a slow response is by definition not late for
anything. When the system stalls, a closed loop **stops sending** — the requests
that would have queued behind the stall are never issued and their latency never
appears anywhere.

The open loop fixes its schedule in advance, so a stall shows up as requests that
were due and had to wait. That 63 ms is real latency a user experienced and no
server-side APM would record.

Note the throughput column too: a closed-loop benchmark reports whatever rate the
system sustained and calls it capacity, which is circular — it measured the rate
it chose to send.

Separate **processes** matter for a duller reason: threads share the GIL with the
server, so every microsecond a client spends parsing is a microsecond the server
cannot run.

**Still not a service latency.** Client and server are one machine with no network
and no serialisation. These are a floor.

> The first version of this generator ran an arrival schedule **and** a think
> sleep in the same loop. That is not a third mode, it is a bug: `intended`
> advanced by the arrival interval while the client also slept for the think time,
> so the schedule fell behind real time by one think per request. It reported a
> **1,432 ms p99 against 0.54 ms of service time** — obviously wrong, and exactly
> the sort of number a benchmark reports with a straight face.

## The HTTP surface

`uvicorn serve:app --port 8010`

Putting it behind HTTP changed almost nothing about the domain logic and forced
every decision an in-process call never has to make:

- **Idempotency keys are required, not optional.** Over a network a retry is
  indistinguishable from a second order, so an optional key makes the default
  behaviour the unsafe one. A repeat returns the *original* order rather than a
  409 — a client that gets an error on its own retry will retry again.
- **The domain returns an outcome; the transport maps it to a status code.**
  `checkout` returns `abandoned_out_of_stock`, which is right for the in-process
  flash-sale harness that counts states, and wrong over HTTP where a 200 says the
  order was placed. Out-of-stock is **409**; a 500 tells the client to retry
  something that cannot succeed.
- **Someone else's order is 404, not 403**, because a 403 confirms the order
  exists — a disclosure when ids are guessable.
- **One connection per thread.** The first version held a single module-level
  SQLite connection, which is fine for every in-process caller in this project and
  is not fine behind a thread pool. SQLite refuses it outright, which is lucky —
  the same mistake with a driver that permits it produces interleaved transactions
  instead.

Auth is a bearer token in a dict. That is not a security design and is not
presented as one; what it demonstrates is the **authorisation boundary** —
`/orders/{id}` checking whether this caller owns that order, which is the check
that leaks data when it is missing.

## The funnel has a time dimension

`funnel_by_bucket` cuts the same event-log query into time buckets, and
`funnel_regression` compares the last few against the rest.

A lifetime funnel answers "how does checkout convert", which nobody asks, because
the answer never changes fast enough to act on. The question people bring to a
funnel is **what changed** — and a single number across all history is
structurally incapable of answering it: a step that fell from 90% to 40% an hour
ago still reads 88% lifetime after a week of healthy traffic. A test asserts
exactly that: the bucketed view detects a planted regression that the lifetime
view reports as fine.

## What is deliberately not here

- **SQLite, not Postgres**, and no Postgres binary is installable in this
  environment. This weakens exactly one claim — the optimistic-vs-pessimistic
  contention benchmark measures retry cost, not row-level concurrency, because
  both queue behind one global write lock. The report says so at the point of the
  claim. The conditional-`UPDATE` shape transfers; the throughput number does not.
- **No tax jurisdiction model.** Rates are per-category. Nexus, destination vs
  origin sourcing and product taxability codes are the actual hard part and are
  entirely absent.
- **Allocation is still a heuristic.** The scarcity penalty is a cover-shortfall
  charge, not a forward-position model — the real version needs a demand forecast
  per DC, which is ML-1's job and is not wired in.
- **No connection pool.** Thread-local connections are fine for SQLite and are how
  a service exhausts a Postgres connection limit under exactly the load it was
  built for.
- **The storefront has no cart, no session and no payment form.** It renders
  inventory and explains the contract; the buying happens through the API.

**Linkage:** SE-2's line-item allocation is what makes the proportional refund
computable, and now also what makes per-line tax computable. `oms.allocate` and
`se2/src/money.py::allocate` are the same largest-remainder algorithm because they
have to agree to the cent.
