"""Tests for the second tranche: allocation, exchanges, repricing, ops metrics."""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src import allocation as AL  # noqa: E402
from src import checkout as co  # noqa: E402
from src import db, inventory, oms  # noqa: E402
from src import lifecycle as LC  # noqa: E402
from src.psp import FakePSP  # noqa: E402


@pytest.fixture()
def con():
    path = os.path.join(tempfile.mkdtemp(), "t.db")
    c = db.init(path, fresh=True)
    AL.init(c)
    LC.init(c)
    yield c
    c.close()


def _order(con, psp, cart, key="k"):
    r = co.checkout(con, psp, "cust", cart, idempotency_key=key)
    return r["order_id"]


def _deliver(con, oid, qtys):
    for st in ("allocated", "picked", "packed"):
        oms.transition(con, oid, st)
    oms.ship(con, oid, qtys)
    oms.transition(con, oid, "delivered")


# --------------------------------------------------------------------------
# allocation
# --------------------------------------------------------------------------
def _three_dcs(con):
    AL.add_location(con, "NEAR", "near", 40.74, -74.17, handling_cost=100)
    AL.add_location(con, "MID", "mid", 39.96, -82.99, handling_cost=100)
    AL.add_location(con, "FAR", "far", 36.17, -115.14, handling_cost=100)


def test_single_dc_that_can_fill_everything_is_one_parcel(con):
    _three_dcs(con)
    db.seed_stock(con, "A", 100, 1000)
    db.seed_stock(con, "B", 100, 1000)
    for loc in ("NEAR", "MID", "FAR"):
        AL.set_stock(con, loc, "A", 50)
        AL.set_stock(con, loc, "B", 50)
    psp = FakePSP(seed=1)
    oid = _order(con, psp, [dict(sku="A", qty=1, unit_price=1000),
                            dict(sku="B", qty=1, unit_price=1000)])
    plan = AL.allocate_order(con, oid, (40.71, -74.01))
    assert plan["parcels"] == 1
    assert set(loc for (_ln, loc) in plan["plan"]) == {"NEAR"}


def test_allocation_splits_when_no_single_dc_can_fill(con):
    _three_dcs(con)
    db.seed_stock(con, "A", 100, 1000)
    db.seed_stock(con, "B", 100, 1000)
    AL.set_stock(con, "NEAR", "A", 5)
    AL.set_stock(con, "NEAR", "B", 0)
    AL.set_stock(con, "MID", "A", 0)
    AL.set_stock(con, "MID", "B", 5)
    AL.set_stock(con, "FAR", "A", 0)
    AL.set_stock(con, "FAR", "B", 0)
    psp = FakePSP(seed=2)
    oid = _order(con, psp, [dict(sku="A", qty=1, unit_price=1000),
                            dict(sku="B", qty=1, unit_price=1000)])
    plan = AL.allocate_order(con, oid, (40.71, -74.01))
    assert plan["parcels"] == 2
    assert plan["unfulfillable"] == []


def test_allocation_covers_every_unit_or_reports_the_shortfall(con):
    _three_dcs(con)
    db.seed_stock(con, "A", 100, 1000)
    for loc in ("NEAR", "MID", "FAR"):
        AL.set_stock(con, loc, "A", 1)
    psp = FakePSP(seed=3)
    oid = _order(con, psp, [dict(sku="A", qty=10, unit_price=1000)])
    plan = AL.allocate_order(con, oid, (40.71, -74.01))
    # 3 units available against 10 needed
    assert plan["plan"] == {}
    assert plan["unfulfillable"] == [(0, "A", 7)]


def test_allocated_units_are_held_and_cannot_be_double_allocated(con):
    _three_dcs(con)
    db.seed_stock(con, "A", 100, 1000)
    AL.set_stock(con, "NEAR", "A", 1)
    AL.set_stock(con, "MID", "A", 0)
    AL.set_stock(con, "FAR", "A", 0)
    psp = FakePSP(seed=4)
    o1 = _order(con, psp, [dict(sku="A", qty=1, unit_price=1000)], key="k1")
    o2 = _order(con, psp, [dict(sku="A", qty=1, unit_price=1000)], key="k2")

    p1 = AL.allocate_order(con, o1, (40.71, -74.01))
    AL.commit_allocation(con, o1, p1["plan"])
    assert AL.available(con, "NEAR", "A") == 0

    p2 = AL.allocate_order(con, o2, (40.71, -74.01))
    assert p2["unfulfillable"], "the second order must see the held unit as gone"


def test_allocation_prefers_cheaper_total_cost_not_fewer_parcels(con):
    """Two near parcels beat one far parcel when distance dominates. The rule
    'minimise splits' cannot express that, which is why this is scored."""
    _three_dcs(con)
    db.seed_stock(con, "A", 100, 1000)
    db.seed_stock(con, "B", 100, 1000)
    AL.set_stock(con, "NEAR", "A", 5)
    AL.set_stock(con, "NEAR", "B", 0)
    AL.set_stock(con, "MID", "A", 0)
    AL.set_stock(con, "MID", "B", 5)
    AL.set_stock(con, "FAR", "A", 5)
    AL.set_stock(con, "FAR", "B", 5)      # FAR could do it in one parcel
    psp = FakePSP(seed=5)
    oid = _order(con, psp, [dict(sku="A", qty=1, unit_price=1000),
                            dict(sku="B", qty=1, unit_price=1000)])
    plan = AL.allocate_order(con, oid, (40.71, -74.01))
    assert plan["parcels"] == 2
    assert "FAR" not in {loc for (_ln, loc) in plan["plan"]}


# --------------------------------------------------------------------------
# exchanges
# --------------------------------------------------------------------------
def test_even_exchange_moves_no_money(con):
    """The claim the report makes. An earlier version compared a tax-INCLUSIVE
    refund to a tax-EXCLUSIVE replacement and netted the tax, so a like-for-like
    swap quietly refunded $2.63."""
    psp = FakePSP(seed=6)
    db.seed_stock(con, "TEE-M", 10, 3000)
    db.seed_stock(con, "TEE-L", 10, 3000)
    oid = _order(con, psp, [dict(sku="TEE-M", qty=1, unit_price=3000)])
    _deliver(con, oid, {0: 1})
    res = LC.create_exchange(con, psp, oid, {0: 1},
                             [dict(sku="TEE-L", qty=1, unit_price=3000)])
    assert res["ok"]
    assert res["net_cents"] == 0
    moved = con.execute("SELECT COUNT(*) n FROM money_movements"
                        " WHERE reason='exchange_net'").fetchone()["n"]
    assert moved == 0
    assert oms.check_ledger_invariants(con) == []


def test_upward_exchange_captures_only_the_difference(con):
    psp = FakePSP(seed=7)
    db.seed_stock(con, "TEE-M", 10, 3000)
    db.seed_stock(con, "TEE-XL", 10, 3600)
    oid = _order(con, psp, [dict(sku="TEE-M", qty=1, unit_price=3000)])
    _deliver(con, oid, {0: 1})
    res = LC.create_exchange(con, psp, oid, {0: 1},
                             [dict(sku="TEE-XL", qty=1, unit_price=3600)])
    assert res["net_cents"] > 0
    rows = con.execute("SELECT kind, amount FROM money_movements"
                       " WHERE reason='exchange_net'").fetchall()
    assert len(rows) == 1
    assert rows[0]["kind"] == "capture"
    assert rows[0]["amount"] == res["net_cents"]


def test_exchange_reserves_the_replacement_before_touching_the_return(con):
    """If the replacement is gone, the customer must keep their original item and
    receive no refund -- rather than being refunded into a stockout."""
    psp = FakePSP(seed=8)
    db.seed_stock(con, "TEE-M", 10, 3000)
    db.seed_stock(con, "TEE-L", 0, 3000)
    oid = _order(con, psp, [dict(sku="TEE-M", qty=1, unit_price=3000)])
    _deliver(con, oid, {0: 1})
    before = oms.financial_summary(con, oid)
    res = LC.create_exchange(con, psp, oid, {0: 1},
                             [dict(sku="TEE-L", qty=1, unit_price=3000)])
    assert res["ok"] is False
    assert res["reason"] == "replacement_out_of_stock"
    after = oms.financial_summary(con, oid)
    assert after["refunded"] == before["refunded"] == 0
    assert con.execute("SELECT state FROM orders WHERE order_id=?",
                       (oid,)).fetchone()["state"] == "delivered"


def test_exchange_creates_a_linked_child_order(con):
    psp = FakePSP(seed=9)
    db.seed_stock(con, "TEE-M", 10, 3000)
    db.seed_stock(con, "TEE-L", 10, 3000)
    oid = _order(con, psp, [dict(sku="TEE-M", qty=1, unit_price=3000)])
    _deliver(con, oid, {0: 1})
    res = LC.create_exchange(con, psp, oid, {0: 1},
                             [dict(sku="TEE-L", qty=1, unit_price=3000)])
    link = con.execute("SELECT * FROM order_links WHERE parent_order_id=?",
                       (oid,)).fetchone()
    assert link["child_order_id"] == res["child_order_id"]
    assert link["link_type"] == "exchange"


# --------------------------------------------------------------------------
# repricing
# --------------------------------------------------------------------------
def test_small_price_rise_is_absorbed(con):
    psp = FakePSP(seed=10)
    db.seed_stock(con, "W", 10, 5000)
    oid = _order(con, psp, [dict(sku="W", qty=1, unit_price=5000)])
    LC.quote(con, oid, 0, 5000)
    d = LC.reprice(con, oid, {0: 5100})
    assert d["verdict"] == "honoured"
    assert d["lines"][0]["charged"] == 5000


def test_large_price_rise_requires_reconfirmation(con):
    psp = FakePSP(seed=11)
    db.seed_stock(con, "W", 10, 5000)
    oid = _order(con, psp, [dict(sku="W", qty=1, unit_price=5000)])
    LC.quote(con, oid, 0, 5000)
    d = LC.reprice(con, oid, {0: 9000})
    assert d["verdict"] == "reconfirm"
    assert d["lines"][0]["charged"] is None


def test_price_drop_is_always_passed_to_the_customer(con):
    psp = FakePSP(seed=12)
    db.seed_stock(con, "W", 10, 5000)
    oid = _order(con, psp, [dict(sku="W", qty=1, unit_price=5000)])
    LC.quote(con, oid, 0, 5000)
    d = LC.reprice(con, oid, {0: 4000})
    assert d["verdict"] == "reduced"
    assert d["lines"][0]["charged"] == 4000
    assert d["delta_cents"] == -1000


def test_repricing_decision_is_recorded_on_the_order(con):
    psp = FakePSP(seed=13)
    db.seed_stock(con, "W", 10, 5000)
    oid = _order(con, psp, [dict(sku="W", qty=1, unit_price=5000)])
    LC.quote(con, oid, 0, 5000)
    LC.reprice(con, oid, {0: 9000})
    ev = con.execute("SELECT detail FROM order_events WHERE order_id=?"
                     " AND to_state='repriced'", (oid,)).fetchone()
    assert ev is not None and "reconfirm" in ev["detail"]


def test_tolerance_is_the_smaller_of_the_two_bounds(con):
    """5% of a $10 item is 50c, which is below the $3 cap -- so the PERCENTAGE
    binds on cheap items and the CAP binds on expensive ones."""
    psp = FakePSP(seed=14)
    db.seed_stock(con, "CHEAP", 10, 1000)
    oid = _order(con, psp, [dict(sku="CHEAP", qty=1, unit_price=1000)])
    LC.quote(con, oid, 0, 1000)
    assert LC.reprice(con, oid, {0: 1040})["verdict"] == "honoured"   # +40c < 50c
    LC.quote(con, oid, 0, 1000)
    assert LC.reprice(con, oid, {0: 1200})["verdict"] == "reconfirm"  # +200c > 50c


# --------------------------------------------------------------------------
# ops metrics
# --------------------------------------------------------------------------
def test_funnel_is_monotone_non_increasing(con):
    psp = FakePSP(seed=15)
    db.seed_stock(con, "A", 50, 1000)
    for i in range(20):
        r = co.checkout(con, psp, "c%d" % i, [dict(sku="A", qty=1, unit_price=1000)],
                        idempotency_key="f%d" % i)
        if r["state"] == "placed" and i % 2 == 0:
            _deliver(con, r["order_id"], {0: 1})
    steps = LC.funnel(con)
    counts = [s["orders"] for s in steps]
    assert counts == sorted(counts, reverse=True), counts


def test_health_reports_open_capture_unknowns(con):
    psp = FakePSP(seed=16, timeout_rate=1.0)
    db.seed_stock(con, "A", 50, 1000)
    for i in range(5):
        co.checkout(con, psp, "c", [dict(sku="A", qty=1, unit_price=1000)],
                    idempotency_key="h%d" % i)
    assert LC.health(con)["capture_unknown_open"] == 5
    co.reconcile_capture_unknown(con, psp)
    assert LC.health(con)["capture_unknown_open"] == 0


def test_reservation_expiry_rate_reflects_released_holds(con):
    db.seed_stock(con, "A", 100, 1000)
    for i in range(10):
        con.execute("INSERT INTO orders(order_id,state,customer_id,created_at,"
                    "updated_at) VALUES (?,'reserved','c',0,0)", ("o%d" % i,))
        inventory.reserve_optimistic(con, "o%d" % i, "A", 1, ttl=-1.0)
    assert LC.health(con)["reservation_expiry_rate"] == 0.0
    inventory.sweep_expired(con)
    assert LC.health(con)["reservation_expiry_rate"] == 1.0
