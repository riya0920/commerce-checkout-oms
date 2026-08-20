"""Which warehouse ships which line -- the decision the first pass asserted.

The first version of this project shipped orders by calling `ship(order, {line:
qty}, warehouse="DC1")` and passing the warehouse in as a string. Nothing decided
it. The "split across warehouses" in the drill was therefore a test fixture
describing a split, not a system computing one, and the README said so.

WHAT ALLOCATION ACTUALLY TRADES OFF
-----------------------------------
Every retailer allocating a multi-line order is balancing three things that
disagree:

  SPLITS      each additional parcel is a real cost -- another pick, another
              pack, another box, another shipping label. Retailers track
              "shipments per order" as a headline metric for this reason.
  DISTANCE    a parcel from a far DC costs more and arrives later, and late
              arrival is a service failure the customer sees.
  BALANCE     draining one DC to avoid a split leaves it unable to serve the
              next order, which is a cost you pay tomorrow rather than today.

This implements a cost-scored search over feasible allocations rather than a
rule, because the rule everyone writes first -- "nearest DC that has everything,
else split" -- is exactly the one that produces four-parcel orders when a
two-parcel answer was available.

HONEST SCOPE: this is a per-order greedy-with-lookahead, not a network
optimisation. It does not consider the orders behind this one, it has no
inventory-position forecast, and it cannot reserve capacity. Those are what turn
allocation into an operations-research problem and none of them are here.
"""
from __future__ import annotations

import itertools
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS locations (
    location_id  TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    lat          REAL NOT NULL,
    lon          REAL NOT NULL,
    handling_cost INTEGER NOT NULL DEFAULT 0   -- cents per parcel from here
);

CREATE TABLE IF NOT EXISTS location_stock (
    location_id  TEXT NOT NULL REFERENCES locations(location_id),
    sku          TEXT NOT NULL,
    on_hand      INTEGER NOT NULL CHECK (on_hand >= 0),
    allocated    INTEGER NOT NULL DEFAULT 0 CHECK (allocated >= 0),
    PRIMARY KEY (location_id, sku)
);

CREATE TABLE IF NOT EXISTS allocations (
    order_id     TEXT NOT NULL,
    line_no      INTEGER NOT NULL,
    location_id  TEXT NOT NULL,
    qty          INTEGER NOT NULL CHECK (qty > 0),
    PRIMARY KEY (order_id, line_no, location_id)
);
"""

# Cost model. These are the merchant's inputs, not physics, and the allocator's
# recommendation is only as defensible as they are -- so they are named
# constants rather than magic numbers buried in a comparison.
PARCEL_COST_CENTS = 650        # pick + pack + box + label for one more parcel
COST_PER_KM_CENTS = 4          # linehaul, per parcel-km


def init(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)


def add_location(con, location_id, name, lat, lon, handling_cost=0):
    con.execute("INSERT OR REPLACE INTO locations VALUES (?,?,?,?,?)",
                (location_id, name, lat, lon, handling_cost))


def set_stock(con, location_id, sku, qty):
    con.execute("INSERT OR REPLACE INTO location_stock"
                "(location_id, sku, on_hand, allocated) VALUES (?,?,?,0)",
                (location_id, sku, qty))


def _distance_km(a, b) -> float:
    """Great-circle, close enough for a cost comparison between DCs."""
    import math
    lat1, lon1 = a
    lat2, lon2 = b
    p = math.pi / 180.0
    h = (0.5 - math.cos((lat2 - lat1) * p) / 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * (1 - math.cos((lon2 - lon1) * p)) / 2)
    return 2 * 6371.0 * math.asin(math.sqrt(max(0.0, min(1.0, h))))


def available(con, location_id, sku) -> int:
    r = con.execute("SELECT on_hand - allocated AS free FROM location_stock"
                    " WHERE location_id=? AND sku=?", (location_id, sku)).fetchone()
    return int(r["free"]) if r else 0


def allocate_order(con: sqlite3.Connection, order_id: str,
                   ship_to: tuple[float, float],
                   max_splits: int = 3) -> dict:
    """Choose locations for every line. Returns the plan and its cost breakdown.

    Searches over subsets of locations up to `max_splits` and scores each by
    parcel cost + distance cost, taking the cheapest FEASIBLE plan. Feasible
    means every unit is covered by free stock at the chosen locations.

    Returns {"plan": {(line_no, location_id): qty}, "parcels": n, "cost": cents,
             "unfulfillable": [(line_no, sku, qty_short)]}
    """
    lines = con.execute(
        "SELECT line_no, sku, qty, qty_shipped FROM order_lines"
        " WHERE order_id=? ORDER BY line_no", (order_id,)).fetchall()
    need = [(r["line_no"], r["sku"], r["qty"] - r["qty_shipped"])
            for r in lines if r["qty"] - r["qty_shipped"] > 0]
    if not need:
        return dict(plan={}, parcels=0, cost=0, unfulfillable=[])

    locs = con.execute("SELECT * FROM locations").fetchall()
    free = {(l["location_id"], sku): available(con, l["location_id"], sku)
            for l in locs for _ln, sku, _q in need}
    dist = {l["location_id"]: _distance_km((l["lat"], l["lon"]), ship_to) for l in locs}
    handling = {l["location_id"]: l["handling_cost"] for l in locs}

    best = None
    loc_ids = [l["location_id"] for l in locs]
    for k in range(1, min(max_splits, len(loc_ids)) + 1):
        for subset in itertools.combinations(loc_ids, k):
            # cheapest-first within the subset so a plan uses the near DC where
            # it can and only falls back to the far one for what is short
            order_by_cost = sorted(subset, key=lambda L: (dist[L], handling[L]))
            remaining = {(ln, sku): q for ln, sku, q in need}
            plan: dict[tuple[int, str], int] = {}
            used = set()
            pool = dict(free)
            for L in order_by_cost:
                for (ln, sku), q in list(remaining.items()):
                    if q <= 0:
                        continue
                    take = min(q, pool.get((L, sku), 0))
                    if take > 0:
                        plan[(ln, L)] = plan.get((ln, L), 0) + take
                        pool[(L, sku)] -= take
                        remaining[(ln, sku)] = q - take
                        used.add(L)
            short = [(ln, sku, q) for (ln, sku), q in remaining.items() if q > 0]
            if short:
                continue
            parcels = len(used)
            cost = sum(PARCEL_COST_CENTS + handling[L]
                       + int(COST_PER_KM_CENTS * dist[L]) for L in used)
            if best is None or cost < best["cost"]:
                best = dict(plan=plan, parcels=parcels, cost=cost, unfulfillable=[])
        if best is not None:
            # A cheaper plan can exist with MORE splits (two near DCs beating one
            # far one), so the search does not stop at the first feasible k --
            # but it does stop once k exceeds max_splits, which is the
            # merchant's tolerance for parcels, not an optimisation limit.
            continue

    if best is None:
        # nothing feasible within max_splits: report what is short rather than
        # silently shipping a partial order
        pool = dict(free)
        short = []
        for ln, sku, q in need:
            have = sum(pool.get((L, sku), 0) for L in loc_ids)
            if have < q:
                short.append((ln, sku, q - have))
        return dict(plan={}, parcels=0, cost=0, unfulfillable=short)
    return best


def commit_allocation(con: sqlite3.Connection, order_id: str, plan: dict) -> None:
    """Persist the plan and hold the stock at each location.

    `allocated` is incremented rather than `on_hand` decremented: the units are
    spoken for but still physically present until the parcel leaves, and an
    allocation that is cancelled has to give them back.
    """
    con.execute("BEGIN IMMEDIATE")
    try:
        for (line_no, loc), qty in plan.items():
            sku = con.execute("SELECT sku FROM order_lines WHERE order_id=? AND line_no=?",
                              (order_id, line_no)).fetchone()["sku"]
            cur = con.execute(
                "UPDATE location_stock SET allocated = allocated + ?"
                " WHERE location_id=? AND sku=? AND on_hand - allocated >= ?",
                (qty, loc, sku, qty))
            if cur.rowcount != 1:
                raise RuntimeError("allocation raced: %s %s x%d" % (loc, sku, qty))
            con.execute("INSERT OR REPLACE INTO allocations VALUES (?,?,?,?)",
                        (order_id, line_no, loc, qty))
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


def parcels_for(con: sqlite3.Connection, order_id: str) -> dict:
    """Group an order's allocation into parcels -- one per location."""
    out: dict[str, dict[int, int]] = {}
    for r in con.execute("SELECT line_no, location_id, qty FROM allocations"
                         " WHERE order_id=? ORDER BY location_id, line_no",
                         (order_id,)):
        out.setdefault(r["location_id"], {})[r["line_no"]] = r["qty"]
    return out
