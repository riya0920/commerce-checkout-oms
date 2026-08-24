# SE-1 — Commerce Core: Checkout, Inventory & Order Management

**Complete against the spec.** Zero oversells at a 1,000-concurrent flash sale by
two mechanisms, capture-unknown reconciliation, a crash-safe sweeper, an
append-only money ledger, **per-line tax adopted from SE-2**, policy tables where
constants used to be, **forward-looking allocation**, a **time-bucketed funnel**,
an HTTP surface with real authorisation, a load generator that measures
coordinated omission, **allocation priced against ML-1's forecast distribution**
rather than a cover ratio, and **the contention drill re-run on real
PostgreSQL**, where the optimistic mechanism turns out to fail in a way SQLite
cannot express.

```bash
python run_flashsale.py      # ~2min  the concurrency drills
python run_complete.py       # ~30s   tax, policies, allocation, the load test
uvicorn serve:app --port 8010   #      storefront, checkout, ops view
python -m pytest tests -q    # 100 tests
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

## The forecast, wired in — allocation against a distribution

The section above priced scarcity as a **cover shortfall**: how far below a
seven-day floor the DC lands. This project called that what it was — *"a heuristic
standing in for a real forward-position model, and the real version needs a demand
forecast per DC, which is ML-1's job"*. It is now ML-1's job done.

This is the **second consumer** of ML-1's block bootstrap. DATA-2 needs demand
*summed* over a lead time, to size an order-up-to level. This needs the **daily
path**, because the question is not "how much will be needed" but "will this DC run
out before the truck arrives" — and a replenishment landing on day 4 decides which
shortfalls happen. ML-1 grew `leadtime_daily_paths` for it, and `leadtime_samples`
now delegates to it so there is one bootstrap and not two.

**What is borrowed is the shape of the uncertainty, not the demand level.** These
SKUs are not M5 items. Each sampled path is rescaled so its mean matches the DC's
own daily demand; what comes from ML-1 is the dispersion and the autocorrelation
the block bootstrap preserves. Importing somebody else's demand level and calling
it a forecast integration would make every number below a property of ML-1's panel.

| scenario | myopic | heuristic | forecast |
|---|---|---|---|
| near thin, far deep and near | NEAR $390.16 | **FAR $339.60** | **FAR $339.60** |
| near thin, far deep and far | NEAR $390.16 | NEAR $390.16 | **FAR $347.60** |
| both comfortable | NEAR $7.06 | NEAR $7.06 | NEAR $7.06 |
| near thin, far also thin | NEAR $652.70 | NEAR $652.70 | NEAR $652.70 |
| **total** | $1,440.08 | $1,389.52 | **$1,346.96** |

Cost is shipping plus realised lost margin. **The forecast policy is priced on one
half of the bootstrap and graded on the other** — a model scored on the samples
that priced it is grading its own assumptions and wins every time without meaning
anything. All three policies face the same held-out futures.

> The first version of this table had all four rows reading `heuristic == myopic`,
> because the near DC never fell below the seven-day floor and the heuristic never
> fired. A comparison against a policy that cannot move is a straw man wearing the
> baseline's answers.

### Where the two differ in kind

**The heuristic cannot see variance.** Two DCs, identical stock and identical days
of cover, different demand variability:

| | heuristic | forecast |
|---|---|---|
| steady demand | 0 cents | 212 cents |
| spiky demand | 0 cents | **1,310 cents** |

Days of cover is a function of the **mean**, so those two DCs are the same number
to it. They are not the same risk.

**Both have a ceiling — that is not what I expected to write, and the measurement
is what corrected it.**

| on hand | heuristic | forecast |
|---|---|---|
| 20 | 420 | 5,400 |
| 12 | 660 | 5,400 |
| 6 | **840** (max) | 5,400 |
| 0 | **840** | **0** |

The forecast penalty saturates at **5,400 = 6 units × 900 cents of margin**. The
difference is *where the ceiling comes from*: the heuristic's 840 is seven days
times a rate, both typed into a signature, bounding the penalty at a number with no
economic meaning — a merchant who widens the shipping gap past **$8.40** silently
turns the whole mechanism off. The forecast's ceiling is the true bound: **you
cannot lose more margin than the units you shipped were worth.**

The last row is the same fact from the other side. An **empty** DC has a forecast
penalty of zero, because shipping from it protects nothing that was not already
lost — while the heuristic still charges its maximum. **Charging to protect stock
that does not exist is a proxy come loose from the thing it proxied for.**

And the obvious criticism of the heuristic is the wrong one: the cover charge is
**continuous** at the floor, rising from zero as cover falls through it. There is no
cliff. There is a kink at a number somebody typed, and saturation a few days later.
A test pins that, so nobody "fixes" a discontinuity that is not there.

**What this does not fix:** the horizon and the stockout cost are still merchant
inputs, and the second is doing more work than anything the forecast contributes —
at a low enough stockout cost every policy here collapses to the myopic one. ML-1
supplies the distribution; it cannot supply what a lost order is worth.

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

## Real Postgres — and a caveat this project repeated for four passes

Every previous README said this:

> *"SQLite, not Postgres, and no Postgres binary is installable in this
> environment. [...] The conditional-`UPDATE` shape transfers; the throughput
> number does not."*

**The first clause was false.** The official PostgreSQL Windows x64 binaries are a
297 MB zip that unpacks and runs `initdb` into a local directory — no installer,
no service registration, no administrator rights. The claim was written in the
first pass and repeated three times without being retested. *A caveat is a claim,
and an unretested claim does not become true by being repeated.*

**And the second clause was understated.** What fails to transfer is not the
throughput number. It is the behaviour.

```bash
.vendor/pgsql/bin/pg_ctl -D .vendor/pgdata -o "-p 5433" start
python run_postgres.py
```

### The same drill, both engines, 16 workers, ample stock

| engine | mechanism | SKUs | granted / attempted | starved | retries | oversells |
|---|---|---|---|---|---|---|
| sqlite | optimistic | 1 | 640 / 640 | **0.0%** | 49 | 0 |
| **postgres** | optimistic | 1 | **277 / 640** | **56.7%** | 427 | 0 |
| sqlite | pessimistic | 1 | 640 / 640 | 0.0% | 0 | 0 |
| postgres | pessimistic | 1 | 640 / 640 | 0.0% | 0 | 0 |
| postgres | optimistic | 4 | 588 / 640 | 8.1% | 847 | 0 |
| postgres | optimistic | 16 | 640 / 640 | 0.0% | 196 | 0 |
| postgres | optimistic | 64 | 640 / 640 | 0.0% | 153 | 0 |

Stock is ample everywhere, so **every "starved" request is one that failed with
stock on the shelf** — the optimistic path exhausting its retry budget. Nothing
here is a real sellout.

**Oversells: 0 on every row of both engines.** That is the part that did transfer,
and it is the part the mechanism exists for.

**Now read the two one-SKU optimistic rows against each other.** Identical code,
identical workload: SQLite starves 0% and Postgres starves 56.7%.

The reason is not that SQLite is better. **SQLite serialises writers**, so a
compare-and-set has nothing to lose a race to — the version cannot move under a
caller who holds the only write lock in the database. **The optimistic
mechanism's characteristic failure is invisible there by construction.** Not
smaller. Invisible.

Which means the earlier benchmark was not a weak measurement of
optimistic-versus-pessimistic concurrency. **It was a measurement of something
else, carrying that name.**

Spreading the same workload across more rows removes it entirely (56.7% → 8.1% →
0.0%), and *that* is the data-model question the old benchmark could not ask:
contention is a property of how many customers want the same SKU, and on SQLite
it was indistinguishable from the engine's own serialisation.

### Throughput: the shape, not the rate

> **A correction to my own first run of this.** The initial harness started its
> timer before the worker threads connected, so ~4.8 s of Postgres connection
> setup was charged to the drill while SQLite's file-open cost nothing. It
> reported **452/s against 32/s** and I put that in this README. Timing now
> starts when the barrier trips, and `setup_seconds` is reported separately.

Absolute rates on one machine are noisy enough that a single run flips the sign —
consecutive runs gave Postgres 917/s and then 563/s on the same cell. So the claim
is made on the **shape**, measured **paired**: the one-row and sixteen-row cells
run back to back within each repetition, so machine drift cancels instead of
landing in the ratio.

Throughput gain from spreading the same workload from 1 row to 16, pessimistic
mechanism, 9 paired repetitions:

| engine | median | range |
|---|---|---|
| sqlite | ~0.8–1.1× | 0.35 – 1.42 |
| **postgres** | **~1.2–1.5×** | 0.97 – 1.78 |

**Postgres scaled more than SQLite in 8 of 9 paired repetitions**, and that count
is the part that reproduces — the median ratio itself moved between two runs of
the same nine-rep measurement.

SQLite's range includes **0.35**: it sometimes goes *slower* with more rows, which
is what "the number of distinct rows is irrelevant to this engine" looks like when
you measure it — noise around 1.0. Its global write lock does not care how many
rows there are. Postgres's row locks do.

**That is SE-2's prediction, tested** — *"writers on different rows queue here and
would proceed in parallel on Postgres."* **It holds in direction and not in
magnitude.** "Proceed in parallel" overstates the size: at 16 workers the
bottleneck is already partly the client, and no engine change moves that.

### The retry budget was calibrated against the wrong engine

`reserve_optimistic` ships with `max_retries=8`. Sixteen workers on one row:

| max_retries | granted / 640 | starved | retries spent |
|---|---|---|---|
| 4 | 195 | 69.5% | 177 |
| **8** (shipped) | **301** | **53.0%** | 532 |
| 16 | 422 | 34.1% | 1,404 |
| 32 | 565 | 11.7% | 3,472 |
| 64 | 623 | **2.7%** | 5,119 |

**It never reaches zero.** Optimistic concurrency converts contention into wasted
work; the budget decides how much waste you buy the tail with, not whether the
tail exists. A hot row needs the pessimistic path or a different data model, and
no retry number fixes it.

The default of 8 was not chosen carelessly — it was chosen against a database
where it could never be tested.

**What still does not transfer:** client and server are one machine over
loopback, fsync off, no connection pool. These are a floor, exactly as the load
generator's numbers are, and changing the engine does not touch that.

## What is deliberately not here

- **The application still runs on SQLite; only the contention drill runs on
  Postgres.** `checkout.py` and the HTTP surface were not ported, so the flash
  sale, the sweeper and the ledger are still measured on the engine whose
  limitation the Postgres pass just documented. Porting the whole application is
  the honest next step and it is not done.
- **No connection pool, loopback only, fsync off.** The Postgres numbers are a
  floor for the same reasons the load generator's are.
- **No tax jurisdiction model.** Rates are per-category. Nexus, destination vs
  origin sourcing and product taxability codes are the actual hard part and are
  entirely absent.
- **Allocation optimises one order at a time.** The forward-position model prices
  the marginal unit correctly and there is still no lookahead over the *queue* of
  orders behind it: allocating ten orders greedily by marginal cost is not the
  same as allocating ten orders. That is a stochastic programme and it is not here.
- **The stockout cost is a constant.** One number for every SKU, and it is the
  input the whole section is most sensitive to.
- **No connection pool.** Thread-local connections are fine for SQLite and are how
  a service exhausts a Postgres connection limit under exactly the load it was
  built for.
- **The storefront has no cart, no session and no payment form.** It renders
  inventory and explains the contract; the buying happens through the API.

**Linkage to ML-1:** `forward_position.py` loads ML-1's block bootstrap by file
path and consumes daily demand paths, which is the same join DATA-2 makes from the
other side and for a different question. Both projects import ML-1's *estimator*
rather than copying it, so if ML-1 changes what the distribution is, both change
with it.

**Linkage:** SE-2's line-item allocation is what makes the proportional refund
computable, and now also what makes per-line tax computable. `oms.allocate` and
`se2/src/money.py::allocate` are the same largest-remainder algorithm because they
have to agree to the cent.
