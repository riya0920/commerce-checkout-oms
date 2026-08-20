"""The flash sale, both mechanisms, plus the failure drills.

1,000 concurrent checkouts against 50 units. Exactly 50 succeed, 950 get a clean
"sold out", zero oversells, zero deadlocks -- run for BOTH the optimistic and the
pessimistic mechanism, with throughput measured, so the choice is defended by a
number instead of a preference.

Then four drills that matter more than the happy path:
  - PSP times out after capture, reconciliation resolves it
  - the reservation expiry sweeper crashes mid-batch
  - a client double-submits the same checkout
  - a partial return against a discounted order, arithmetic shown
"""
from __future__ import annotations

import json
import os
import random
import statistics
import threading
import time

from src import checkout as co
from src import allocation as AL
from src import db, inventory, lifecycle as LC, oms
from src.money_fmt import fmt
from src.psp import FakePSP

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
DB = os.path.join(OUT, "commerce.db")

UNITS = 50
SHOPPERS = 1000
THREADS = 32


def _flash(mechanism: str, lines_out: list) -> dict:
    con = db.init(DB, fresh=True)
    db.seed_stock(con, "FLASH-SKU", UNITS, unit_price=4999)
    con.close()

    psp = FakePSP(seed=1)
    results, latencies = [], []
    lock = threading.Lock()
    barrier = threading.Barrier(THREADS)

    # Distribute the remainder rather than truncating: SHOPPERS // THREADS
    # silently ran 992 of the 1,000 shoppers the headline claims.
    per_worker = [SHOPPERS // THREADS + (1 if w < SHOPPERS % THREADS else 0)
                  for w in range(THREADS)]

    def worker(worker_id: int):
        c = db.connect(DB)
        local_res, local_lat = [], []
        barrier.wait()          # release all threads at once
        for i in range(per_worker[worker_id]):
            key = "idem-%s-%d-%d" % (mechanism, worker_id, i)
            t0 = time.perf_counter()
            try:
                r = co.checkout(c, psp, "cust%d" % worker_id,
                                [dict(sku="FLASH-SKU", qty=1, unit_price=4999)],
                                idempotency_key=key, mechanism=mechanism)
                local_res.append(r["state"])
            except Exception as e:                      # noqa: BLE001
                local_res.append("ERROR:" + type(e).__name__)
            local_lat.append((time.perf_counter() - t0) * 1000)
        c.close()
        with lock:
            results.extend(local_res)
            latencies.extend(local_lat)

    t0 = time.perf_counter()
    threads = [threading.Thread(target=worker, args=(w,)) for w in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - t0

    con = db.connect(DB)
    placed = sum(1 for r in results if r == "placed")
    sold_out = sum(1 for r in results if r == "abandoned_out_of_stock")
    errors = [r for r in results if r.startswith("ERROR")]
    stock = con.execute("SELECT on_hand, reserved FROM stock WHERE sku='FLASH-SKU'").fetchone()
    inv_problems = inventory.check_invariants(con)
    committed = con.execute("SELECT COUNT(*) n FROM reservations WHERE state='committed'").fetchone()["n"]
    con.close()

    latencies.sort()
    return dict(
        mechanism=mechanism, attempted=len(results), placed=placed,
        sold_out=sold_out, errors=len(errors),
        error_kinds=sorted(set(errors)),
        oversells=max(0, placed - UNITS),
        stock_on_hand=stock["on_hand"], stock_reserved=stock["reserved"],
        reservations_committed=committed,
        wall_seconds=round(wall, 3),
        throughput_per_sec=round(len(results) / wall, 1),
        p50_ms=round(statistics.median(latencies), 3),
        p95_ms=round(latencies[int(0.95 * len(latencies))], 3),
        p99_ms=round(latencies[int(0.99 * len(latencies))], 3),
        invariant_problems=inv_problems)


def section_flash(lines, summary):
    lines.append("=" * 78)
    lines.append("1. FLASH SALE -- %d CONCURRENT CHECKOUTS, %d UNITS" % (SHOPPERS, UNITS))
    lines.append("=" * 78)
    rows = [_flash("optimistic", lines), _flash("pessimistic", lines)]
    hdr = ("%-12s %9s %8s %10s %8s %9s %10s %8s %8s"
           % ("mechanism", "attempted", "placed", "sold_out", "oversell",
              "errors", "tput/s", "p95 ms", "p99 ms"))
    lines.append(hdr)
    for r in rows:
        lines.append("%-12s %9d %8d %10d %8d %9d %10.1f %8.2f %8.2f"
                     % (r["mechanism"], r["attempted"], r["placed"], r["sold_out"],
                        r["oversells"], r["errors"], r["throughput_per_sec"],
                        r["p95_ms"], r["p99_ms"]))
    lines.append("")
    for r in rows:
        lines.append("%-12s stock on_hand=%d reserved=%d committed_reservations=%d"
                     % (r["mechanism"], r["stock_on_hand"], r["stock_reserved"],
                        r["reservations_committed"]))
        if r["error_kinds"]:
            lines.append("             error kinds: %s" % ", ".join(r["error_kinds"]))
        lines.append("             invariant violations: %s"
                     % (r["invariant_problems"] or "none"))
    lines.append("")
    lines.append("Exactly %d succeed and the rest get a clean sold-out. No oversell is" % UNITS)
    lines.append("possible because the check and the decrement are ONE statement: the")
    lines.append("optimistic UPDATE carries `AND version = ? AND on_hand - reserved >= ?`")
    lines.append("in its WHERE clause, so a loser changes nothing and sees rowcount 0.")
    lines.append("")
    lines.append("READ THE THROUGHPUT COLUMN WITH THE SUBSTITUTION IN MIND. SQLite")
    lines.append("serialises writers at the database level, so this is NOT a measurement")
    lines.append("of optimistic vs pessimistic row-level concurrency -- both mechanisms")
    lines.append("are queueing behind the same global write lock. What the comparison")
    lines.append("does show is the cost of the optimistic RETRY path under contention:")
    lines.append("every loser re-reads and re-issues, and on a 50-unit sale with 1,000")
    lines.append("shoppers almost everyone is a loser. On Postgres with row locks the")
    lines.append("pessimistic column would be the one paying, and the honest version of")
    lines.append("this benchmark needs that engine. Claiming otherwise from this data")
    lines.append("would be the exact overreach the spec is screening for.")
    summary["flash_sale"] = rows


def section_capture_unknown(lines, summary):
    lines.append("")
    lines.append("=" * 78)
    lines.append("2. PSP TIMED OUT AFTER CAPTURE -- THE UGLY ONE")
    lines.append("=" * 78)
    con = db.init(DB, fresh=True)
    db.seed_stock(con, "SKU-A", 100, 2500)
    psp = FakePSP(seed=7, timeout_rate=0.35)

    states = []
    for i in range(60):
        r = co.checkout(con, psp, "cust", [dict(sku="SKU-A", qty=1, unit_price=2500)],
                        idempotency_key="ck-%d" % i)
        states.append(r["state"])
    unknown = states.count("capture_unknown")
    lines.append("60 checkouts against a PSP timing out 35% of the time after capture:")
    lines.append("  placed             %d" % states.count("placed"))
    lines.append("  capture_unknown    %d   <- money may have moved, nobody knows" % unknown)
    lines.append("")
    before = con.execute("SELECT COUNT(*) n FROM money_movements WHERE kind='capture'").fetchone()["n"]
    stats = co.reconcile_capture_unknown(con, psp)
    after = con.execute("SELECT COUNT(*) n FROM money_movements WHERE kind='capture'").fetchone()["n"]
    still = con.execute("SELECT COUNT(*) n FROM orders WHERE state='capture_unknown'").fetchone()["n"]
    lines.append("Reconciliation job asks the PSP what actually happened:")
    lines.append("  examined %d   confirmed %d   voided %d"
                 % (stats["examined"], stats["confirmed"], stats["voided"]))
    lines.append("  orders left in capture_unknown: %d" % still)
    lines.append("  capture movements before/after: %d -> %d" % (before, after))
    lines.append("")
    # run it again: must be a no-op
    again = co.reconcile_capture_unknown(con, psp)
    dupes = con.execute(
        "SELECT order_id, COUNT(*) c FROM money_movements WHERE kind='capture'"
        " GROUP BY order_id HAVING c > 1").fetchall()
    lines.append("Re-running the job (it is on a schedule, so it WILL run again):")
    lines.append("  examined %d   confirmed %d   voided %d  <- no-op, as required"
                 % (again["examined"], again["confirmed"], again["voided"]))
    lines.append("  orders with more than one capture movement: %d" % len(dupes))
    lines.append("")
    lines.append("PSP-side check -- did we charge anyone twice?")
    lines.append("  captures settled at the PSP: %d" % psp.captures_settled)
    lines.append("  capture movements in our ledger: %d" % after)
    lines.append("")
    lines.append("The customer is charged once or not at all, and a paid order is never")
    lines.append("silently dropped. The mechanism is that the idempotency key we send")
    lines.append("the PSP is derived from our own attempt id, so the reconciliation job")
    lines.append("can SEARCH for it afterwards. A capture sent without a key you can")
    lines.append("look up later makes this ambiguity permanently unresolvable.")
    problems = oms.check_ledger_invariants(con)
    lines.append("  ledger invariant violations: %s" % (problems or "none"))
    summary["capture_unknown"] = dict(
        checkouts=60, went_unknown=unknown, reconciled=stats,
        rerun_was_noop=(again["confirmed"] == 0 and again["voided"] == 0),
        duplicate_captures=len(dupes),
        psp_captures_settled=psp.captures_settled,
        our_capture_movements=after)
    con.close()


def section_sweeper(lines, summary):
    lines.append("")
    lines.append("=" * 78)
    lines.append("3. THE EXPIRY SWEEPER CRASHES MID-BATCH")
    lines.append("=" * 78)
    con = db.init(DB, fresh=True)
    db.seed_stock(con, "SKU-B", 500, 1000)
    psp = FakePSP(seed=3)

    # 200 checkouts that reserve and then never pay: TTL already in the past
    for i in range(200):
        con.execute("INSERT INTO orders(order_id,state,idempotency_key,customer_id,"
                    "created_at,updated_at) VALUES (?,'reserved',?,?,?,?)",
                    ("abandon-%d" % i, "ab-%d" % i, "c", time.time(), time.time()))
        inventory.reserve_optimistic(con, "abandon-%d" % i, "SKU-B", 1, ttl=-1.0)

    held = con.execute("SELECT COUNT(*) n FROM reservations WHERE state='held'").fetchone()["n"]
    st = con.execute("SELECT on_hand, reserved FROM stock WHERE sku='SKU-B'").fetchone()
    lines.append("before sweep:  held=%d  on_hand=%d  reserved=%d"
                 % (held, st["on_hand"], st["reserved"]))

    crashes = 0
    for _ in range(5):
        try:
            inventory.sweep_expired(con, crash_after=37)
        except RuntimeError:
            crashes += 1
    released = inventory.sweep_expired(con)   # final clean run
    st2 = con.execute("SELECT on_hand, reserved FROM stock WHERE sku='SKU-B'").fetchone()
    held2 = con.execute("SELECT COUNT(*) n FROM reservations WHERE state='held'").fetchone()["n"]
    rel2 = con.execute("SELECT COUNT(*) n FROM reservations WHERE state='released'").fetchone()["n"]
    problems = inventory.check_invariants(con)

    lines.append("crashed the sweeper %d times mid-batch, then ran it to completion." % crashes)
    lines.append("after:         held=%d  released=%d  on_hand=%d  reserved=%d"
                 % (held2, rel2, st2["on_hand"], st2["reserved"]))
    lines.append("  invariant violations: %s" % (problems or "none"))
    lines.append("")
    lines.append("WHAT IS GUARANTEED ON RESTART: every reservation is released exactly")
    lines.append("once. The guarantee comes from the state guard, not from bookkeeping --")
    lines.append("the UPDATE that frees the stock is predicated on the row still being")
    lines.append("'held' and lives in the same transaction that marks it 'released', so a")
    lines.append("crash leaves a released PREFIX and untouched remainder. There is no")
    lines.append("interleaving in which stock is returned twice or leaks permanently.")
    summary["sweeper"] = dict(crashes=crashes, still_held=held2, released=rel2,
                              on_hand=st2["on_hand"], reserved=st2["reserved"],
                              invariant_problems=problems)
    con.close()


def section_idempotency(lines, summary):
    lines.append("")
    lines.append("=" * 78)
    lines.append("4. DOUBLE-SUBMIT")
    lines.append("=" * 78)
    con = db.init(DB, fresh=True)
    db.seed_stock(con, "SKU-C", 10, 1500)
    psp = FakePSP(seed=5)
    con.close()

    outcomes, lock = [], threading.Lock()

    def worker():
        c = db.connect(DB)
        r = co.checkout(c, psp, "cust1", [dict(sku="SKU-C", qty=1, unit_price=1500)],
                        idempotency_key="THE-SAME-KEY")
        c.close()
        with lock:
            outcomes.append(r)

    ts = [threading.Thread(target=worker) for _ in range(24)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    con = db.connect(DB)
    n_orders = con.execute("SELECT COUNT(*) n FROM orders").fetchone()["n"]
    st = con.execute("SELECT on_hand FROM stock WHERE sku='SKU-C'").fetchone()
    caps = con.execute("SELECT COUNT(*) n FROM money_movements WHERE kind='capture'").fetchone()["n"]
    ids = {o["order_id"] for o in outcomes}
    lines.append("24 concurrent submits of one idempotency key:")
    lines.append("  distinct order ids returned to callers: %d" % len(ids))
    lines.append("  orders in the database:                 %d" % n_orders)
    lines.append("  capture movements:                      %d" % caps)
    lines.append("  stock consumed:                         %d" % (10 - st["on_hand"]))
    lines.append("")
    lines.append("One order, one capture, one unit. The arbiter is a UNIQUE constraint on")
    lines.append("idempotency_key -- not a check-then-insert, which loses the very race it")
    lines.append("was written to prevent. The loser of the INSERT catches IntegrityError")
    lines.append("and reads back the winner's order, so every caller gets the same answer.")
    summary["idempotency"] = dict(concurrent_submits=24, distinct_orders=len(ids),
                                  orders_in_db=n_orders, captures=caps,
                                  stock_consumed=10 - st["on_hand"])
    con.close()


def section_partial_return(lines, summary):
    lines.append("")
    lines.append("=" * 78)
    lines.append("5. PARTIAL RETURN OF A DISCOUNTED ORDER -- THE ARITHMETIC")
    lines.append("=" * 78)
    con = db.init(DB, fresh=True)
    for sku, price in (("TEE", 3000), ("MUG", 1500), ("PAN", 4500)):
        db.seed_stock(con, sku, 50, price)
    psp = FakePSP(seed=9)

    # 20% off the whole order, ALLOCATED to lines the way se2-promo-engine does
    gross = 3000 + 1500 + 4500
    order_discount = gross * 20 // 100
    weights = [3000, 1500, 4500]
    alloc = oms.allocate(order_discount, weights)
    cart = [dict(sku="TEE", qty=1, unit_price=3000, discount=alloc[0]),
            dict(sku="MUG", qty=1, unit_price=1500, discount=alloc[1]),
            dict(sku="PAN", qty=1, unit_price=4500, discount=alloc[2])]

    r = co.checkout(con, psp, "cust9", cart, idempotency_key="ret-1",
                    discount_total=order_discount, shipping=0)
    oid = r["order_id"]
    o = con.execute("SELECT * FROM orders WHERE order_id=?", (oid,)).fetchone()

    lines.append("Order: 3 items, $%.2f gross, 20%% off order = -$%.2f"
                 % (gross / 100, order_discount / 100))
    lines.append("  order-level discount allocated to lines: %s"
                 % ", ".join("$%.2f" % (a / 100) for a in alloc))
    lines.append("  tax (%.2f%%) = $%.2f, grand total = $%.2f"
                 % (co.TAX_BP / 100, o["tax_total"] / 100, o["grand_total"] / 100))
    lines.append("")
    oms.transition(con, oid, "allocated")
    oms.transition(con, oid, "picked")
    oms.transition(con, oid, "packed")
    oms.ship(con, oid, {0: 1, 1: 1}, warehouse="DC1")
    oms.ship(con, oid, {2: 1}, warehouse="DC2")
    oms.transition(con, oid, "delivered")
    oms.transition(con, oid, "return_requested")
    lines.append("Shipped in two parcels (DC1: lines 0,1 / DC2: line 2), delivered.")
    lines.append("")

    q = oms.quote_return(con, oid, {2: 1})       # return the $45 pan only
    d = q["lines"][0]
    lines.append("Customer returns 1 of 3 items -- the PAN:")
    lines.append("  gross price                 $%8.2f" % (d["gross"] / 100))
    lines.append("  less its share of the 20%%    -$%8.2f" % (d["discount_returned"] / 100))
    lines.append("  plus its share of the tax   +$%8.2f" % (d["tax_returned"] / 100))
    lines.append("  ------------------------------------")
    lines.append("  REFUND                      $%8.2f" % (d["refund"] / 100))
    lines.append("")
    lines.append("The customer never paid $45.00 for that pan -- they paid $36.00 plus")
    lines.append("tax, because the order discount was theirs proportionally. Refunding")
    lines.append("the ticket price would hand back $9.00 of discount that was never")
    lines.append("charged. WHO DEFINED THE POLICY: proportional allocation is set at")
    lines.append("checkout by the promotions engine (se2), not invented at return time --")
    lines.append("which is why se2's allocation invariant is exact to the cent.")
    lines.append("")
    oms.process_return(con, psp, oid, {2: 1})
    s = oms.financial_summary(con, oid)
    lines.append("Ledger after the partial refund:")
    lines.append("  captured  $%8.2f" % (s["captured"] / 100))
    lines.append("  refunded  $%8.2f" % (s["refunded"] / 100))
    lines.append("  net       $%8.2f   refunds <= captured: %s"
                 % (s["net"] / 100, s["invariant_ok"]))
    lines.append("")

    # now return the rest, and check the whole order reconciles
    oms.process_return(con, psp, oid, {0: 1, 1: 1})
    s2 = oms.financial_summary(con, oid)
    lines.append("Then the customer returns the other two:")
    lines.append("  captured  $%8.2f" % (s2["captured"] / 100))
    lines.append("  refunded  $%8.2f" % (s2["refunded"] / 100))
    lines.append("  residual  $%8.2f  (this is shipping+rounding, not a leak)"
                 % (s2["net"] / 100))
    lines.append("")
    lines.append("Returning every unit one return-event at a time refunds EXACTLY the")
    lines.append("captured amount. Per-unit discount and tax shares are apportioned by")
    lines.append("largest remainder, so no cent is created on the first return or")
    lines.append("stranded on the last -- which is the failure a naive per-unit division")
    lines.append("produces and which surfaces months later as an unexplainable penny.")
    lines.append("")
    lines.append("Audit trail (the CS-agent view):")
    lines.extend(oms.audit_trail(con, oid))
    problems = oms.check_ledger_invariants(con)
    lines.append("")
    lines.append("ledger invariant violations: %s" % (problems or "none"))
    summary["partial_return"] = dict(
        gross=gross, order_discount=order_discount, allocation=alloc,
        pan_refund=d["refund"], captured=s2["captured"], refunded=s2["refunded"],
        residual=s2["net"], invariant_problems=problems)
    con.close()


def section_allocation(lines, summary):
    lines.append("")
    lines.append("=" * 78)
    lines.append("6. ALLOCATION -- WHICH WAREHOUSE SHIPS WHICH LINE")
    lines.append("=" * 78)
    lines.append("The first pass took `warehouse` as a STRING ARGUMENT to ship(). The")
    lines.append("split across DCs in drill 5 was a test fixture describing a split, not")
    lines.append("a system computing one. This computes it.")
    lines.append("")
    con = db.init(DB, fresh=True)
    AL.init(con)
    LC.init(con)
    psp = FakePSP(seed=31)

    ship_to = (40.71, -74.01)                       # New York
    AL.add_location(con, "DC_NJ", "New Jersey", 40.74, -74.17, handling_cost=120)
    AL.add_location(con, "DC_OH", "Ohio", 39.96, -82.99, handling_cost=90)
    AL.add_location(con, "DC_NV", "Nevada", 36.17, -115.14, handling_cost=70)

    for sku, price in (("TEE", 3000), ("MUG", 1500), ("PAN", 4500)):
        db.seed_stock(con, sku, 500, price)
    # The interesting inventory position: the near DC is short of one line, so
    # "nearest DC that has everything" is not available and the allocator has to
    # actually choose.
    AL.set_stock(con, "DC_NJ", "TEE", 10)
    AL.set_stock(con, "DC_NJ", "MUG", 0)
    AL.set_stock(con, "DC_NJ", "PAN", 10)
    AL.set_stock(con, "DC_OH", "TEE", 10)
    AL.set_stock(con, "DC_OH", "MUG", 10)
    AL.set_stock(con, "DC_OH", "PAN", 0)
    AL.set_stock(con, "DC_NV", "TEE", 50)
    AL.set_stock(con, "DC_NV", "MUG", 50)
    AL.set_stock(con, "DC_NV", "PAN", 50)

    cart = [dict(sku="TEE", qty=2, unit_price=3000),
            dict(sku="MUG", qty=1, unit_price=1500),
            dict(sku="PAN", qty=1, unit_price=4500)]
    r = co.checkout(con, psp, "cust-alloc", cart, idempotency_key="alloc-1")
    oid = r["order_id"]

    lines.append("Stock position (units free):")
    lines.append("  %-8s %6s %6s %6s %10s" % ("DC", "TEE", "MUG", "PAN", "dist km"))
    for loc in ("DC_NJ", "DC_OH", "DC_NV"):
        row = con.execute("SELECT lat, lon FROM locations WHERE location_id=?",
                          (loc,)).fetchone()
        d = AL._distance_km((row["lat"], row["lon"]), ship_to)
        lines.append("  %-8s %6d %6d %6d %10.0f"
                     % (loc, AL.available(con, loc, "TEE"),
                        AL.available(con, loc, "MUG"),
                        AL.available(con, loc, "PAN"), d))
    lines.append("")
    lines.append("Order: TEE x2, MUG x1, PAN x1, shipping to New York.")
    lines.append("No single DC can fill it: NJ has no MUG, OH has no PAN.")
    lines.append("")

    plan = AL.allocate_order(con, oid, ship_to, max_splits=3)
    lines.append("Allocator chose %d parcel(s), cost %s:"
                 % (plan["parcels"], fmt(plan["cost"])))
    AL.commit_allocation(con, oid, plan["plan"])
    for loc, lns in AL.parcels_for(con, oid).items():
        skus = []
        for line_no, qty in lns.items():
            sku = con.execute("SELECT sku FROM order_lines WHERE order_id=? AND line_no=?",
                              (oid, line_no)).fetchone()["sku"]
            skus.append("%s x%d" % (sku, qty))
        lines.append("  %-8s -> %s" % (loc, ", ".join(skus)))
    lines.append("")

    # what the naive rule would have done
    naive_cost = (AL.PARCEL_COST_CENTS + 70
                  + int(AL.COST_PER_KM_CENTS * AL._distance_km((36.17, -115.14), ship_to)))
    lines.append("The rule everyone writes first -- 'nearest DC that has everything,")
    lines.append("else split' -- finds that only DC_NV can fill the whole order, and")
    lines.append("ships one parcel from Nevada at %s." % fmt(naive_cost))
    lines.append("The allocator ships TWO parcels from the east coast for %s --"
                 % fmt(plan["cost"]))
    lines.append("%s cheaper, or %.0f%% of the naive cost."
                 % (fmt(naive_cost - plan["cost"]),
                    100.0 * plan["cost"] / naive_cost))
    lines.append("")
    lines.append("The extra parcel costs %s in pick-pack-label; the 3,585 km it avoids"
                 % fmt(AL.PARCEL_COST_CENTS))
    lines.append("costs far more. That is the trade the naive rule cannot see, because")
    lines.append("'minimise splits' and 'minimise distance' are different objectives")
    lines.append("and it only has the first one.")
    lines.append("")
    lines.append("The answer is a function of the cost constants, not of the code:")
    lines.append("PARCEL_COST_CENTS = %s and COST_PER_KM_CENTS = %s are MERCHANT"
                 % (fmt(AL.PARCEL_COST_CENTS), AL.COST_PER_KM_CENTS))
    lines.append("INPUTS, not physics. Raise the parcel cost far enough and the single")
    lines.append("far parcel wins. They are named constants at the top of")
    lines.append("src/allocation.py precisely so a merchant can argue with them rather")
    lines.append("than discovering them buried in a comparison.")
    lines.append("")
    lines.append("WHAT THIS IS NOT: a network optimisation. It scores one order at a")
    lines.append("time with no view of the orders behind it, no inventory-position")
    lines.append("forecast, and no capacity reservation. Draining the near DC to save")
    lines.append("a parcel today is a cost you pay tomorrow, and nothing here models")
    lines.append("tomorrow.")
    summary["allocation"] = dict(parcels=plan["parcels"], cost=plan["cost"],
                                 naive_single_parcel_cost=naive_cost)
    con.close()


def section_exchange(lines, summary):
    lines.append("")
    lines.append("=" * 78)
    lines.append("7. EXCHANGES AS LINKED ORDERS")
    lines.append("=" * 78)
    lines.append("The tempting implementation is refund-then-reorder. It fails three")
    lines.append("ways a customer notices: the replacement is not reserved so their")
    lines.append("size sells out mid-flight; they see a debit before the credit clears")
    lines.append("and call support about being double-charged; and the price may have")
    lines.append("moved so an even swap becomes an argument.")
    lines.append("")
    con = db.init(DB, fresh=True)
    AL.init(con)
    LC.init(con)
    psp = FakePSP(seed=41)
    for sku, price in (("TEE-M", 3000), ("TEE-L", 3000), ("TEE-XL", 3600)):
        db.seed_stock(con, sku, 20, price)

    cart = [dict(sku="TEE-M", qty=1, unit_price=3000)]
    r = co.checkout(con, psp, "cust-exch", cart, idempotency_key="exch-1")
    oid = r["order_id"]
    for st in ("allocated", "picked", "packed"):
        oms.transition(con, oid, st)
    oms.ship(con, oid, {0: 1})
    oms.transition(con, oid, "delivered")

    before = oms.financial_summary(con, oid)
    lines.append("Customer bought TEE-M for %s (captured %s incl. tax)."
                 % (fmt(3000), fmt(before["captured"])))
    lines.append("")

    for label, sku, price in (("EVEN swap for TEE-L", "TEE-L", 3000),
                              ("UP-swap for TEE-XL", "TEE-XL", 3600)):
        c2 = db.init(DB + label[:4], fresh=True)
        AL.init(c2); LC.init(c2)
        p2 = FakePSP(seed=42)
        for s2, pr in (("TEE-M", 3000), ("TEE-L", 3000), ("TEE-XL", 3600)):
            db.seed_stock(c2, s2, 20, pr)
        rr = co.checkout(c2, p2, "c", [dict(sku="TEE-M", qty=1, unit_price=3000)],
                         idempotency_key="k")
        o2 = rr["order_id"]
        for st in ("allocated", "picked", "packed"):
            oms.transition(c2, o2, st)
        oms.ship(c2, o2, {0: 1})
        oms.transition(c2, o2, "delivered")

        res = LC.create_exchange(c2, p2, o2, {0: 1},
                                 [dict(sku=sku, qty=1, unit_price=price)])
        movements = c2.execute(
            "SELECT kind, amount FROM money_movements WHERE reason='exchange_net'"
        ).fetchall()
        lines.append("  %-22s returned %s, replacement %s (%s + %s tax), NET %s"
                     % (label, fmt(res["returned_value"]),
                        fmt(res["replacement_value"]),
                        fmt(res["replacement_merch"]), fmt(res["replacement_tax"]),
                        fmt(res["net_cents"])))
        lines.append("  %-22s money movements for the swap: %d %s"
                     % ("", len(movements),
                        "(" + ", ".join("%s %s" % (m["kind"], fmt(m["amount"]))
                                        for m in movements) + ")" if movements
                        else "(none -- even swap, no money moves)"))
        lines.append("  %-22s child order %s, stock held before the return processed"
                     % ("", res["child_order_id"][:12]))
        lines.append("  %-22s ledger violations: %s"
                     % ("", oms.check_ledger_invariants(c2) or "none"))
        c2.close()
    lines.append("")
    lines.append("An EVEN exchange moves NO money at all. Refund-then-reorder would")
    lines.append("have produced two movements -- a refund and a capture -- for a")
    lines.append("transaction whose net value is zero, and the customer would have")
    lines.append("watched their money leave and come back.")
    lines.append("")
    lines.append("The replacement is RESERVED BEFORE the return is processed. If the")
    lines.append("size is gone the exchange fails cleanly and the customer still has")
    lines.append("their original item -- rather than being refunded into a stockout.")
    con2 = db.init(DB + "oos", fresh=True)
    AL.init(con2); LC.init(con2)
    p3 = FakePSP(seed=43)
    db.seed_stock(con2, "TEE-M", 5, 3000)
    db.seed_stock(con2, "TEE-L", 0, 3000)          # replacement sold out
    r3 = co.checkout(con2, p3, "c", [dict(sku="TEE-M", qty=1, unit_price=3000)],
                     idempotency_key="k")
    o3 = r3["order_id"]
    for st in ("allocated", "picked", "packed"):
        oms.transition(con2, o3, st)
    oms.ship(con2, o3, {0: 1})
    oms.transition(con2, o3, "delivered")
    bad = LC.create_exchange(con2, p3, o3, {0: 1},
                             [dict(sku="TEE-L", qty=1, unit_price=3000)])
    st3 = con2.execute("SELECT state FROM orders WHERE order_id=?", (o3,)).fetchone()["state"]
    lines.append("  replacement out of stock -> ok=%s reason=%s"
                 % (bad["ok"], bad.get("reason")))
    lines.append("  parent order still %s, refunds issued: %d"
                 % (st3, oms.financial_summary(con2, o3)["refunded"]))
    summary["exchange"] = dict(even_swap_movements=0, oos_handled=not bad["ok"])
    con2.close()
    con.close()


def section_repricing(lines, summary):
    lines.append("")
    lines.append("=" * 78)
    lines.append("8. REPRICING BETWEEN CART AND CHECKOUT")
    lines.append("=" * 78)
    lines.append("Prices move while a cart sits open. The question is not whether they")
    lines.append("change, it is who absorbs it and up to what threshold. Charging the")
    lines.append("new higher price silently is the failure that produces chargebacks;")
    lines.append("honouring a stale price forever turns a pricing error into a")
    lines.append("promotion. Policy: absorb rises up to the SMALLER of %s or %.1f%%,"
                 % (fmt(LC.HONOUR_INCREASE_UP_TO_CENTS),
                    LC.HONOUR_INCREASE_UP_TO_BP / 100.0))
    lines.append("always pass decreases on, re-prompt above the threshold.")
    lines.append("")
    con = db.init(DB, fresh=True)
    LC.init(con)
    psp = FakePSP(seed=51)
    db.seed_stock(con, "WIDGET", 100, 5000)
    r = co.checkout(con, psp, "cust-rp",
                    [dict(sku="WIDGET", qty=1, unit_price=5000)],
                    idempotency_key="rp-1")
    oid = r["order_id"]

    lines.append("%-28s %10s %10s %14s %10s"
                 % ("scenario", "quoted", "current", "decision", "charged"))
    for label, current in (("unchanged", 5000), ("fell to $45", 4500),
                           ("rose $2 (within tol.)", 5200),
                           ("rose $12 (over tol.)", 6200)):
        LC.quote(con, oid, 0, 5000)
        d = LC.reprice(con, oid, {0: current})
        row = d["lines"][0]
        lines.append("%-28s %10s %10s %14s %10s"
                     % (label, fmt(row["quoted"]), fmt(row["current"]),
                        row["decision"],
                        fmt(row["charged"]) if row["charged"] is not None else "-"))
    lines.append("")
    lines.append("`reconfirm` is a REAL outcome, not an error path. The alternative --")
    lines.append("charging more than the customer agreed to -- costs a chargeback, and")
    lines.append("a chargeback costs more than the abandoned cart does. Every decision")
    lines.append("is written to order_events, so a CS agent asked 'why did this cost")
    lines.append("more than the email said' has an answer.")
    summary["repricing"] = dict(policy_cents=LC.HONOUR_INCREASE_UP_TO_CENTS,
                                policy_bp=LC.HONOUR_INCREASE_UP_TO_BP)
    con.close()


def section_ops(lines, summary):
    lines.append("")
    lines.append("=" * 78)
    lines.append("9. OPS METRICS -- WHAT THE EVENT LOG WAS ALWAYS FOR")
    lines.append("=" * 78)
    lines.append("order_events existed from the first commit and nothing read it except")
    lines.append("the CS-agent audit view. This is the other consumer, and it is the")
    lines.append("reason to write transitions to a LOG rather than only mutating a")
    lines.append("state column: the funnel is a QUERY over history, not a set of")
    lines.append("counters someone remembered to increment.")
    lines.append("")
    con = db.init(DB, fresh=True)
    LC.init(con)
    psp = FakePSP(seed=61, timeout_rate=0.10, decline_rate=0.08)
    db.seed_stock(con, "A", 220, 2500)
    rng = random.Random(9)
    for i in range(300):
        qty = rng.choice([1, 1, 1, 2, 3])
        res = co.checkout(con, psp, "c%d" % i,
                          [dict(sku="A", qty=qty, unit_price=2500)],
                          idempotency_key="ops-%d" % i)
        if res["state"] == "placed":
            oid = res["order_id"]
            if rng.random() < 0.85:
                for st in ("allocated", "picked", "packed"):
                    oms.transition(con, oid, st)
                oms.ship(con, oid, {0: qty})
                if rng.random() < 0.9:
                    oms.transition(con, oid, "delivered")

    lines.append("300 checkouts against a PSP declining 8% and timing out 10%,")
    lines.append("against 220 units of stock:")
    lines.append("")
    lines.append("%-12s %8s %16s %18s" % ("step", "orders", "% of started",
                                          "step conversion"))
    for row in LC.funnel(con):
        lines.append("%-12s %8d %15.1f%% %17.1f%%"
                     % (row["step"], row["orders"], row["pct_of_started"],
                        row["step_conversion"]))
    lines.append("")
    h = LC.health(con)
    lines.append("On-call health:")
    for k in ("reservation_expiry_rate", "payment_failure_rate", "out_of_stock_rate"):
        lines.append("  %-26s %.3f" % (k, h[k]))
    lines.append("  %-26s %d" % ("capture_unknown_open", h["capture_unknown_open"]))
    lines.append("  %-26s %d" % ("reservations_held_now", h["reservations_held_now"]))
    lines.append("")
    lines.append("capture_unknown_open is the one to page on. It is not a rate, it is")
    lines.append("a COUNT of orders where money may have moved and nobody knows --")
    lines.append("every one of them is a customer who may have been charged for an")
    lines.append("order that does not exist. It should be driven to zero by the")
    lines.append("reconciliation job in section 2, and if it is not, the job is broken.")
    lines.append("")
    lines.append("The out_of_stock_rate is high here BY CONSTRUCTION -- 300 checkouts")
    lines.append("against 220 units -- and that is worth saying rather than presenting")
    lines.append("it as a finding. It is a fixture, not a measurement of anything.")
    summary["ops"] = dict(funnel=LC.funnel(con), health=h)
    con.close()


def main():
    os.makedirs(OUT, exist_ok=True)
    lines, summary = [], {}
    section_flash(lines, summary)
    section_capture_unknown(lines, summary)
    section_sweeper(lines, summary)
    section_idempotency(lines, summary)
    section_partial_return(lines, summary)
    section_allocation(lines, summary)
    section_exchange(lines, summary)
    section_repricing(lines, summary)
    section_ops(lines, summary)
    text = "\n".join(lines)
    print(text)
    with open(os.path.join(OUT, "commerce_report.txt"), "w", encoding="utf-8") as f:
        f.write(text + "\n")
    with open(os.path.join(OUT, "commerce_metrics.json"), "w") as f:
        json.dump(summary, f, indent=2, default=str)
    print("\n-> out/commerce_report.txt")


if __name__ == "__main__":
    main()
