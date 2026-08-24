"""The completion pass: per-line tax, policy tables, forward-looking allocation,
a time-bucketed funnel, and a load test that is actually a load test.

Run after nothing -- it builds its own database. Writes out/complete_report.txt.
"""
from __future__ import annotations

import json
import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from src import allocation as ALLOC   # noqa: E402
from src import checkout as CO        # noqa: E402
from src import db as DB              # noqa: E402
from src import loadgen as LG         # noqa: E402
from src import forward_position as FP
from src import policies as POL       # noqa: E402
from src import psp as PSP            # noqa: E402
from src import taxpolicy as TAX      # noqa: E402
from src.money_fmt import fmt         # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
LOAD_DB = os.path.join(OUT, "loadtest.db")


def main():
    os.makedirs(OUT, exist_ok=True)
    lines, summary = [], {}

    def emit(s=""):
        print(s)
        lines.append(s)

    emit("=" * 78)
    emit("SE-1 COMPLETION PASS")
    emit("=" * 78)
    emit("")

    # ======================================================================
    emit("=" * 78)
    emit("A. PER-LINE TAX -- TWO OF MY OWN PROJECTS DISAGREED ABOUT A NUMBER")
    emit("=" * 78)
    emit("The previous README said it plainly: 'SE-2 now models per-line rates;")
    emit("SE-1 has not adopted them, so the two projects disagree about tax and")
    emit("SE-1 is the wrong one.' Two services in one portfolio computed a")
    emit("different tax on the same cart.")
    emit("")
    cart = [
        dict(sku="TEE-BLUE", category="apparel", gross=4998, discount=800),
        dict(sku="MILK", category="grocery", gross=1347, discount=216),
        dict(sku="HEADPHONES", category="electronics", gross=8999, discount=1440),
    ]
    res = TAX.cart_tax(cart)
    emit("  %-12s %10s %10s %10s %8s %10s"
         % ("line", "gross", "discount", "taxable", "rate bp", "tax"))
    for ln in res["lines"]:
        emit("  %-12s %10s %10s %10s %8d %10s"
             % (ln["sku"], fmt(ln["gross"]), fmt(-ln["discount"]),
                fmt(ln["taxable"]), ln["rate_bp"], fmt(ln["tax"])))
    emit("  %-12s %10s %10s %10s %8s %10s"
         % ("TOTAL", "", "", "", "", fmt(res["tax_total"])))
    emit("")
    emit("A single cart rate would have charged %s -- %s more, by taxing the"
         % (fmt(res["single_rate_would_charge"]),
            fmt(res["overcharge_from_single_rate"])))
    emit("exempt groceries.")
    emit("")
    emit("  THERE IS NO CORRECT SINGLE CART RATE when the lines differ, which is")
    emit("  exactly why the order-level discount has to be ALLOCATED to lines")
    emit("  before tax is computable at all. That is the same allocation SE-2's")
    emit("  promo engine and this project's partial refunds both stand on: get it")
    emit("  wrong and you are wrong twice, in two systems, months apart.")
    emit("")
    mf = TAX.cart_tax([dict(sku="TEE", category="apparel", gross=4998,
                            discount=800, manufacturer_funded=True)])
    rf = TAX.cart_tax([dict(sku="TEE", category="apparel", gross=4998,
                            discount=800)])
    emit("  MANUFACTURER vs RETAILER discount on the same line:")
    emit("    retailer-funded     taxable %s  tax %s"
         % (fmt(rf["lines"][0]["taxable"]), fmt(rf["tax_total"])))
    emit("    manufacturer-funded taxable %s  tax %s"
         % (fmt(mf["lines"][0]["taxable"]), fmt(mf["tax_total"])))
    emit("  A retailer discount reduces the taxable receipt; a manufacturer")
    emit("  coupon does not, because the retailer is reimbursed. %s of tax on one"
         % fmt(mf["tax_total"] - rf["tax_total"]))
    emit("  line, and it is a flag rather than prose because somebody will ask.")
    emit("")
    summary["tax"] = res

    # ======================================================================
    emit("=" * 78)
    emit("B. POLICIES THAT WERE CONSTANTS")
    emit("=" * 78)
    emit("Repricing between cart and checkout, per CATEGORY and per TIER:")
    emit("")
    emit("  %-12s %-9s %9s %9s %12s %10s"
         % ("category", "tier", "quoted", "current", "decision", "charged"))
    rows = []
    for cat, tier, quoted, current in (
            ("apparel", "standard", 5000, 5200),
            ("apparel", "gold", 5000, 5200),
            ("electronics", "standard", 120000, 122000),
            ("electronics", "gold", 120000, 122000),
            ("grocery", "standard", 449, 470),
            ("apparel", "standard", 5000, 4500)):
        d = POL.reprice_decision(quoted, current, cat, tier)
        rows.append(dict(category=cat, tier=tier, quoted=quoted, current=current,
                         **{k: v for k, v in d.items() if k != "rule"}))
        emit("  %-12s %-9s %9s %9s %12s %10s"
             % (cat, tier, fmt(quoted), fmt(current), d["decision"],
                fmt(d["charge"]) if d["charge"] is not None else "-"))
    emit("")
    emit("  The same +$20 rise is HONOURED for a standard apparel shopper and")
    emit("  RECONFIRMED for a standard electronics one, because the electronics")
    emit("  rule caps at 2% and $20 on a $1,200 item is under the cash cap but")
    emit("  over the percentage. The tolerance is the SMALLER of the two, so the")
    emit("  percentage binds on expensive items and the cash cap on cheap ones.")
    emit("")
    emit("  The tier column is a real and slightly uncomfortable decision: a")
    emit("  programme that absorbs more for high-value customers IS price")
    emit("  discrimination by tenure. It is legal and widespread, and it belongs")
    emit("  in a table somebody signed off rather than an if-statement somebody")
    emit("  wrote.")
    emit("")
    summary["repricing"] = rows

    emit("-" * 78)
    emit("Returns: shipping, windows and restocking.")
    emit("")
    emit("  %-12s %6s %6s %12s %12s %12s %10s"
         % ("category", "days", "full", "merch", "shipping", "restock", "refund"))
    rrows = []
    for cat, days, full in (("apparel", 10, True), ("apparel", 10, False),
                            ("apparel", 90, True), ("electronics", 3, True),
                            ("electronics", 20, True), ("grocery", 3, True)):
        q = POL.return_quote(category=cat, days_since_delivery=days,
                             is_full_return=full, merch_refund_cents=5000,
                             shipping_paid_cents=795)
        rrows.append(dict(category=cat, days=days, full=full, **q))
        emit("  %-12s %6d %6s %12s %12s %12s %10s"
             % (cat, days, full, fmt(5000),
                fmt(q.get("shipping_refund", 0)), fmt(-q.get("restocking_fee", 0)),
                fmt(q["refund"]) if q["eligible"] else "REFUSED"))
    emit("")
    emit("  SHIPPING COMES BACK ON A FULL RETURN AND NOT ON A PARTIAL ONE, and")
    emit("  the reason is not arbitrary: a partial return still required the")
    emit("  parcel, so the shipping was consumed. A full return means the")
    emit("  shipment should not have happened.")
    emit("")
    emit("  Electronics carry a restocking fee outside a 7-day grace period, so")
    emit("  the fast returns that correlate with a sizing or spec mistake are")
    emit("  free and the slow ones -- which correlate with use -- are not.")
    emit("")
    emit("  The 90-day apparel row is REFUSED, and refusing is a policy outcome")
    emit("  the API has to be able to express. The previous version had no window")
    emit("  at all, which is not a generous policy, it is an absent one.")
    emit("")
    summary["returns"] = rrows

    emit("-" * 78)
    emit("Exchanges, including the case the previous pass got wrong:")
    emit("")
    for name, ret, rep in (("even swap", 3263, 3263),
                           ("upgrade", 3263, 3915),
                           ("cheaper, above the floor", 3915, 3263),
                           ("cheaper, below the floor", 3263, 3200)):
        s = POL.exchange_settlement(ret, rep)
        emit("  %-26s %s -> %s : %-13s %s"
             % (name, fmt(ret), fmt(rep), s["movement"], s["note"]))
    emit("")
    emit("  A CHEAPER REPLACEMENT IS NOT SIMPLY A NET REFUND. Below a floor the")
    emit("  refund costs more to process than it returns -- payment fees, a")
    emit("  statement line the customer queries, a support contact -- so small")
    emit("  differences become store credit. The floor is a merchant input, and")
    emit("  stating it is the point: the alternative is a silent rounding that")
    emit("  customers notice and support cannot explain.")
    emit("")

    # ======================================================================
    emit("=" * 78)
    emit("C. ALLOCATION THAT CAN SEE TOMORROW")
    emit("=" * 78)
    # A SWEEP OVER DISTANCE, not one pair. The first version compared a scarce
    # DC 14 km away against a deep one 3,585 km away, where the shipping
    # difference is $143 and no plausible scarcity penalty could ever flip it --
    # so the section demonstrated a mechanism that could not matter. The question
    # is not "does the penalty change this decision", it is "at what distance
    # does it start to".
    near = POL.DCPosition("DC_NEAR", on_hand={"TEE": 12}, committed={"TEE": 0},
                          km=14, daily_demand={"TEE": 4.0})
    emit("Near DC: %d units, %.1f days of cover. Alternative DC is deep in stock"
         % (near.on_hand["TEE"], near.days_of_cover("TEE")))
    emit("and further away. Where does the far DC start to win?")
    emit("")
    emit("  %-10s %10s %12s %12s %14s %12s"
         % ("far DC km", "near ship", "far ship", "near penalty",
            "near total", "winner"))
    arows = []
    flip_at = None
    for km in (100, 300, 600, 900, 1500, 3585):
        far = POL.DCPosition("DC_FAR", on_hand={"TEE": 400}, committed={"TEE": 0},
                             km=km, daily_demand={"TEE": 5.0})
        near_ship = ALLOC.PARCEL_COST_CENTS + int(near.km * ALLOC.COST_PER_KM_CENTS)
        far_ship = ALLOC.PARCEL_COST_CENTS + int(far.km * ALLOC.COST_PER_KM_CENTS)
        pen = POL.scarcity_penalty_cents(near, "TEE", 2)
        near_total = near_ship + pen
        winner = "near" if near_total <= far_ship else "FAR"
        if winner == "near" and flip_at is None:
            pass
        if winner == "FAR" and flip_at is None:
            flip_at = km
        arows.append(dict(far_km=km, near_ship=near_ship, far_ship=far_ship,
                          near_penalty=pen, near_total=near_total, winner=winner))
        emit("  %-10d %10s %12s %12s %14s %12s"
             % (km, fmt(near_ship), fmt(far_ship), fmt(pen), fmt(near_total),
                winner))
    emit("")
    myopic_always_near = all(r["near_ship"] <= r["far_ship"] for r in arows)
    emit("  A MYOPIC SCORER PICKS THE NEAR DC ON EVERY ROW (%s), because shipping"
         % ("confirmed" if myopic_always_near else "not here"))
    emit("  from 14 km is cheaper than shipping from anywhere. The forward-aware")
    emit("  scorer adds %s for draining a DC that has %.1f days of cover, and"
         % (fmt(arows[0]["near_penalty"]), near.days_of_cover("TEE")))
    if flip_at is not None:
        emit("  that flips the decision once the far DC is within %d km." % flip_at)
        emit("")
        emit("  The flip point is the useful output, not the winner. It says how")
        emit("  much scarcity is worth in shipping-distance terms, which is a")
        emit("  number a network planner can argue with -- and it is entirely a")
        emit("  function of two merchant constants (%s per parcel, %s per km)."
             % (fmt(ALLOC.PARCEL_COST_CENTS), fmt(ALLOC.COST_PER_KM_CENTS)))
    else:
        emit("  it never flips over this range. Worth reporting rather than")
        emit("  hiding: a penalty that cannot change a decision at any plausible")
        emit("  distance is a penalty nobody needs, and on this cost structure")
        emit("  shipping dominates scarcity everywhere.")
    emit("")
    emit("  HONEST LIMIT: the penalty is a cover-shortfall heuristic, not a")
    emit("  forward-position model. The real version needs a demand forecast per")
    emit("  DC, which is ML-1's job and is not wired in here.")
    emit("")
    summary["allocation"] = arows

    # ======================================================================
    emit("=" * 78)
    emit("D. A LOAD TEST THAT IS ACTUALLY A LOAD TEST")
    emit("=" * 78)
    emit("The previous one was a thread pool: no think time, no separate client")
    emit("process, so p99 contained Python's own scheduler. Three distortions,")
    emit("all in the same direction.")
    emit("")
    if os.path.exists(LOAD_DB):
        os.remove(LOAD_DB)
    con = DB.init(LOAD_DB, fresh=True)
    DB.seed_stock(con, "FLASH", 120, 1000)
    con.close()

    rows = []
    for label, mode, think, arrival in (
            ("closed loop (think 40ms)", "closed", 0.04, 0.0),
            ("open loop (arrivals 6ms)", "open", 0.0, 0.006)):
        t0 = time.time()
        res = LG.run("flash_sale_checkout", (LOAD_DB, "optimistic"),
                     n_clients=6, requests_per_client=30, mode=mode,
                     arrival_interval=arrival, think_mean=think, seed=3)
        s_ = LG.summarise(res)
        s_["label"] = label
        s_["mode"] = mode
        s_["wall_seconds"] = time.time() - t0
        rows.append(s_)
        emit("  %-26s throughput %7.1f/s   ok %.3f"
             % (label, s_["throughput_per_s"], s_["ok_rate"]))
        emit("  %-26s service   p50 %6.2f  p95 %6.2f  p99 %6.2f ms"
             % ("", s_["service_p50"], s_["service_p95"], s_["service_p99"]))
        emit("  %-26s perceived p50 %6.2f  p95 %6.2f  p99 %6.2f ms"
             % ("", s_["perceived_p50"], s_["perceived_p95"], s_["perceived_p99"]))
        emit("")
    closed, openl = rows
    emit("  COORDINATED OMISSION, MEASURED.")
    emit("")
    emit("  closed loop : perceived p99 %.2f ms, service p99 %.2f ms, gap %.2f"
         % (closed["perceived_p99"], closed["service_p99"],
            closed["omission_gap_p99"]))
    emit("  open loop   : perceived p99 %.2f ms, service p99 %.2f ms, gap %.2f"
         % (openl["perceived_p99"], openl["service_p99"],
            openl["omission_gap_p99"]))
    emit("")
    emit("  The closed loop's gap is ZERO BY CONSTRUCTION, and that is the whole")
    emit("  problem with it rather than a good property: a request is 'due' when")
    emit("  the client becomes free, so a client that is blocked on a slow")
    emit("  response is by definition not late for anything. When the system")
    emit("  stalls, the closed loop STOPS SENDING -- the requests that would have")
    emit("  queued behind the stall are never issued, and their latency never")
    emit("  appears anywhere.")
    emit("")
    emit("  The open loop fixes its schedule in advance, so a stall shows up as")
    emit("  requests that were due and had to wait. That difference is real")
    emit("  latency a user experienced and no server-side APM would ever record.")
    emit("")
    emit("  Note the throughput column as well: %.1f/s closed against %.1f/s open."
         % (closed["throughput_per_s"], openl["throughput_per_s"]))
    emit("  A closed-loop benchmark reports whatever throughput the system can")
    emit("  sustain and calls it capacity, which is circular -- it measured the")
    emit("  rate it chose to send.")
    emit("")
    emit("  Separate PROCESSES matter for a duller reason: threads share the GIL")
    emit("  with the server, so every microsecond a client spends parsing is a")
    emit("  microsecond the server cannot run. The old number contained the")
    emit("  client's own cost.")
    emit("")
    emit("  STILL NOT A SERVICE LATENCY. Client and server are one machine with")
    emit("  no network and no serialisation. These are a floor, and quoting them")
    emit("  as production latency would be the same mistake in a new costume.")
    emit("")
    summary["load"] = rows

    # ======================================================================
    emit("=" * 78)
    emit("E. THE FORECAST, WIRED IN -- ALLOCATION AGAINST A DISTRIBUTION")
    emit("=" * 78)
    emit("The section above priced scarcity as a COVER SHORTFALL: how far below a")
    emit("seven-day floor the DC lands. This project called that what it is --")
    emit("'a heuristic standing in for a real forward-position model, and the real")
    emit("version needs a demand forecast per DC, which is ML-1's job'. It is now")
    emit("ML-1's job done.")
    emit("")
    if not FP.ml1_available():
        emit("  ML-1 artifacts not found; run ml1-forecast-to-price/run_forecast.py")
        emit("  first. This section needs out/quantile_raw.pkl.")
        summary["forward_position"] = dict(available=False)
    else:
        pool = FP.error_pool()
        emit("  Pooled standardised forecast errors from ML-1: %d, sd %.3f."
             % (len(pool), pool.std()))
        emit("  What is borrowed is the SHAPE of the uncertainty -- dispersion and")
        emit("  the autocorrelation the block bootstrap preserves. The demand LEVEL")
        emit("  stays SE-1's, because these SKUs are not M5 items and importing")
        emit("  somebody else's level would make every number below a property of")
        emit("  ML-1's panel rather than of this allocator.")
        emit("")
        HORIZON = 14
        STOCKOUT_CENTS = 900          # margin lost on an order that cannot ship
        SHIP_BASE, SHIP_PER_KM = 650, 4
        # The near DC has to land BELOW the seven-day floor for the heuristic to
        # fire at all, and the far DC has to be close enough that the heuristic's
        # penalty could plausibly outweigh the shipping gap. Scenarios where the
        # heuristic can never move would make it a straw man wearing the myopic
        # policy's answers -- the first version of this table did exactly that,
        # and all four rows read heuristic == myopic.
        scenarios = [
            # name, near on-hand, near daily, far on-hand, far daily, far km
            ("near thin, far deep and near",   20, 4.0, 260, 4.0, 100),
            ("near thin, far deep and far",    20, 4.0, 260, 4.0, 300),
            ("both comfortable",              220, 4.0, 260, 4.0, 100),
            ("near thin, far also thin",       20, 4.0,  26, 4.0, 100),
        ]
        rows = []
        for name, n_oh, n_dd, f_oh, f_dd, f_km in scenarios:
            near_paths = FP.demand_paths(n_dd, HORIZON, pool, n_samples=1200,
                                         seed=1)
            far_paths = FP.demand_paths(f_dd, HORIZON, pool, n_samples=1200,
                                        seed=2)
            n_price, n_score = FP.split_paths(near_paths)
            f_price, f_score = FP.split_paths(far_paths)
            near_ship = SHIP_BASE + SHIP_PER_KM * 14
            far_ship = SHIP_BASE + SHIP_PER_KM * f_km
            qty = 6

            near_dc = POL.DCPosition(name="NEAR", on_hand={"SKU": n_oh},
                                     km=14.0, daily_demand={"SKU": n_dd})
            far_dc = POL.DCPosition(name="FAR", on_hand={"SKU": f_oh},
                                    km=float(f_km), daily_demand={"SKU": f_dd})

            choices = {}
            choices["myopic"] = "NEAR" if near_ship <= far_ship else "FAR"

            h_near = near_ship + POL.scarcity_penalty_cents(near_dc, "SKU", qty)
            h_far = far_ship + POL.scarcity_penalty_cents(far_dc, "SKU", qty)
            choices["heuristic"] = "NEAR" if h_near <= h_far else "FAR"

            f_near = near_ship + FP.scarcity_penalty_forecast_cents(
                n_price, n_oh, qty, STOCKOUT_CENTS)
            f_far = far_ship + FP.scarcity_penalty_forecast_cents(
                f_price, f_oh, qty, STOCKOUT_CENTS)
            choices["forecast"] = "NEAR" if f_near <= f_far else "FAR"

            # score every policy on the SAME held-out futures
            def realised(pick):
                ship = near_ship if pick == "NEAR" else far_ship
                n_left = n_oh - (qty if pick == "NEAR" else 0)
                f_left = f_oh - (qty if pick == "FAR" else 0)
                lost = (FP.expected_shortfall(n_score, n_left)
                        + FP.expected_shortfall(f_score, f_left))
                return ship + lost * STOCKOUT_CENTS

            row = dict(scenario=name)
            for pol in ("myopic", "heuristic", "forecast"):
                row[pol] = choices[pol]
                row[pol + "_cost"] = realised(choices[pol]) / 100.0
            rows.append(row)
        A = pd.DataFrame(rows)
        emit(A.to_string(index=False, float_format=lambda x: "%9.2f" % x))
        emit("")
        emit("  Cost is shipping plus realised lost margin, in dollars, scored on")
        emit("  the HELD-OUT half of the bootstrap. The forecast policy is priced")
        emit("  on one half and graded on the other; a model scored on the samples")
        emit("  that priced it is grading its own assumptions, and would win every")
        emit("  time without meaning anything.")
        emit("")
        tot = {p: float(A[p + "_cost"].sum()) for p in
               ("myopic", "heuristic", "forecast")}
        emit("  Total across the four scenarios:  " + "   ".join(
            "%s $%.2f" % (k, v) for k, v in tot.items()))
        best = min(tot, key=tot.get)
        emit("  Cheapest: %s." % best)
        emit("")
        disagree = A[(A.heuristic != A.forecast)]
        emit("  The two informed policies disagree on %d of %d scenarios."
             % (len(disagree), len(A)))
        if len(disagree):
            for _, r in disagree.iterrows():
                emit("    %-26s heuristic->%-4s $%7.2f   forecast->%-4s $%7.2f"
                     % (r.scenario, r.heuristic, r.heuristic_cost,
                        r.forecast, r.forecast_cost))
        emit("")
        emit("WHERE THE TWO DIFFER IN KIND, not just in number:")
        emit("")
        v_lo = FP.demand_paths(4.0, HORIZON, pool * 0.35, n_samples=1200, seed=7)
        v_hi = FP.demand_paths(4.0, HORIZON, pool * 1.9, n_samples=1200, seed=7)
        steady = POL.DCPosition(name="STEADY", on_hand={"SKU": 70}, km=14.0,
                                daily_demand={"SKU": 4.0})
        spiky = POL.DCPosition(name="SPIKY", on_hand={"SKU": 70}, km=14.0,
                               daily_demand={"SKU": 4.0})
        emit("  Two DCs, identical stock (70) and identical days of cover (17.5),")
        emit("  different demand VARIABILITY:")
        emit("")
        emit("    heuristic penalty, steady : %6d cents"
             % POL.scarcity_penalty_cents(steady, "SKU", 6))
        emit("    heuristic penalty, spiky  : %6d cents"
             % POL.scarcity_penalty_cents(spiky, "SKU", 6))
        emit("    forecast penalty, steady  : %6d cents"
             % FP.scarcity_penalty_forecast_cents(v_lo, 70, 6, STOCKOUT_CENTS))
        emit("    forecast penalty, spiky   : %6d cents"
             % FP.scarcity_penalty_forecast_cents(v_hi, 70, 6, STOCKOUT_CENTS))
        emit("")
        emit("  THE HEURISTIC CANNOT TELL THEM APART. Days of cover is a function")
        emit("  of the MEAN, so two DCs with the same cover and different variance")
        emit("  are the same number to it. They are not the same risk, and the")
        emit("  difference is not small.")
        emit("")
        emit("  The second structural difference is a CEILING, and it is the one")
        emit("  that decides whether the heuristic can ever change an answer.")
        emit("")
        cap = POL.scarcity_penalty_cents(
            POL.DCPosition(name="X", on_hand={"SKU": 0}, km=14.0,
                           daily_demand={"SKU": 4.0}), "SKU", 6)
        emit("  Largest penalty the cover-shortfall charge can EVER return: %d"
             % cap)
        emit("  cents -- seven days times 120 a day, reached when the DC is empty.")
        emit("  So it cannot flip a decision against a shipping gap wider than")
        emit("  $%.2f, no matter how badly the DC is about to run out." % (cap / 100))
        emit("")
        for oh in (20, 12, 6, 0):
            emit("    on hand %3d  ->  heuristic %4d cents, forecast %5d cents"
                 % (oh, POL.scarcity_penalty_cents(
                        POL.DCPosition(name="X", on_hand={"SKU": oh}, km=14.0,
                                       daily_demand={"SKU": 4.0}), "SKU", 6),
                    FP.scarcity_penalty_forecast_cents(
                        FP.demand_paths(4.0, HORIZON, pool, n_samples=1200,
                                        seed=1)[600:], oh, 6, STOCKOUT_CENTS)))
        emit("")
        emit("  BOTH HAVE A CEILING. That is not what I expected to write, and the")
        emit("  table above is what corrected it -- the forecast column saturates")
        emit("  at %d cents, which is %d units times %d cents of margin."
             % (6 * STOCKOUT_CENTS, 6, STOCKOUT_CENTS))
        emit("")
        emit("  The difference is WHERE the ceiling comes from. The heuristic's")
        emit("  %d is seven days times a rate, both typed into a signature; it" % cap)
        emit("  bounds the penalty at a number with no economic meaning, and a")
        emit("  merchant who widens the shipping gap past $%.2f silently turns the"
             % (cap / 100))
        emit("  whole mechanism off. The forecast's ceiling is the true bound: you")
        emit("  cannot lose more margin than the units you shipped were worth, so")
        emit("  it saturates exactly when shipping these units guarantees losing")
        emit("  all of them.")
        emit("")
        emit("  The last row is the same fact from the other side. An EMPTY DC has")
        emit("  a forecast penalty of zero, because shipping from it protects")
        emit("  nothing that was not already lost -- while the heuristic still")
        emit("  charges its maximum %d. Charging to protect stock that does not" % cap)
        emit("  exist is the clearest case of a proxy having come loose from the")
        emit("  thing it was proxying for.")
        emit("")
        emit("  And the obvious criticism of the heuristic is the wrong one: the")
        emit("  cover charge is CONTINUOUS at the seven-day floor, rising from zero")
        emit("  as cover falls through it. There is no cliff. What there is, is a")
        emit("  kink at a number somebody typed and a saturation point a few days")
        emit("  later.")
        emit("")
        emit("  WHAT THIS DOES NOT FIX: the horizon and the stockout cost are")
        emit("  still merchant inputs, and the second one is doing more work than")
        emit("  anything the forecast contributes -- at a low enough stockout cost")
        emit("  every policy here collapses to the myopic one. ML-1 supplies the")
        emit("  distribution; it cannot supply what a lost order is worth.")
        emit("")
        summary["forward_position"] = dict(
            available=True, pool=len(pool), totals=tot,
            scenarios=A.to_dict("records"))

    with open(os.path.join(OUT, "complete_report.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    with open(os.path.join(OUT, "complete_metrics.json"), "w") as f:
        json.dump(summary, f, indent=2, default=float)
    print("\n-> out/complete_report.txt")


if __name__ == "__main__":
    main()
