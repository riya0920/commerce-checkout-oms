"""Guards on the claims. The state machine is property-tested; the money is not
allowed to drift.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src import checkout as co  # noqa: E402
from src import db, inventory, oms  # noqa: E402
from src.psp import FakePSP, PSPDeclined  # noqa: E402


@pytest.fixture()
def con():
    path = os.path.join(tempfile.mkdtemp(), "t.db")
    c = db.init(path, fresh=True)
    yield c
    c.close()


# --------------------------------------------------------------------------
# money apportionment
# --------------------------------------------------------------------------
@given(st.integers(0, 10 ** 8),
       st.lists(st.integers(0, 10 ** 5), min_size=1, max_size=10))
@settings(max_examples=300, deadline=None)
def test_allocation_is_exact(total, weights):
    parts = oms.allocate(total, weights)
    assert sum(parts) == total


@given(st.integers(0, 500_000), st.integers(0, 200_000), st.integers(1, 25))
@settings(max_examples=300, deadline=None)
def test_unit_shares_sum_to_the_line_totals(discount, tax, qty):
    """Returning every unit separately must refund exactly the line's discount and
    tax -- no cent created on the first return, none stranded on the last."""
    d_units, t_units = oms.line_unit_shares(discount, tax, qty)
    assert sum(d_units) == discount
    assert sum(t_units) == tax
    assert len(d_units) == len(t_units) == qty


# --------------------------------------------------------------------------
# state machine
# --------------------------------------------------------------------------
def test_illegal_transitions_are_unreachable(con):
    con.execute("INSERT INTO orders(order_id,state,customer_id,created_at,updated_at)"
                " VALUES ('o1','placed','c',0,0)")
    with pytest.raises(oms.IllegalTransition):
        oms.transition(con, "o1", "delivered")     # skipped four states
    with pytest.raises(oms.IllegalTransition):
        oms.transition(con, "o1", "returned")
    oms.transition(con, "o1", "allocated")         # legal
    assert con.execute("SELECT state FROM orders").fetchone()["state"] == "allocated"


def test_terminal_states_have_no_exits(con):
    for s in oms.TERMINAL:
        assert oms.TRANSITIONS[s] == set()
    con.execute("INSERT INTO orders(order_id,state,customer_id,created_at,updated_at)"
                " VALUES ('o2','cancelled','c',0,0)")
    with pytest.raises(oms.IllegalTransition):
        oms.transition(con, "o2", "placed")


def test_every_transition_target_is_a_known_state():
    known = set(oms.TRANSITIONS)
    for frm, tos in oms.TRANSITIONS.items():
        for to in tos:
            assert to in known, "%s -> %s targets an undeclared state" % (frm, to)


# --------------------------------------------------------------------------
# inventory
# --------------------------------------------------------------------------
def test_reservation_never_oversells_under_contention(con):
    path = con.execute("PRAGMA database_list").fetchone()["file"]
    db.seed_stock(con, "S", 25)
    granted, lock = [], threading.Lock()

    def worker(w):
        c = db.connect(path)
        got = 0
        for i in range(20):
            try:
                inventory.reserve_optimistic(c, "o%d-%d" % (w, i), "S", 1)
                got += 1
            except inventory.OutOfStock:
                pass
        c.close()
        with lock:
            granted.append(got)

    ts = [threading.Thread(target=worker, args=(w,)) for w in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sum(granted) == 25
    assert inventory.check_invariants(con) == []


def test_release_is_idempotent(con):
    db.seed_stock(con, "S", 5)
    rid, _ = inventory.reserve_optimistic(con, "o", "S", 2)
    assert inventory.release_reservation(con, rid) is True
    assert inventory.release_reservation(con, rid) is False   # second call: no-op
    assert con.execute("SELECT reserved FROM stock WHERE sku='S'").fetchone()["reserved"] == 0
    assert inventory.check_invariants(con) == []


def test_commit_is_idempotent(con):
    db.seed_stock(con, "S", 5)
    rid, _ = inventory.reserve_optimistic(con, "o", "S", 2)
    assert inventory.commit_reservation(con, rid) is True
    assert inventory.commit_reservation(con, rid) is False
    r = con.execute("SELECT on_hand, reserved FROM stock WHERE sku='S'").fetchone()
    assert (r["on_hand"], r["reserved"]) == (3, 0)


def test_sweeper_crash_releases_each_reservation_exactly_once(con):
    db.seed_stock(con, "S", 100)
    for i in range(60):
        con.execute("INSERT INTO orders(order_id,state,customer_id,created_at,updated_at)"
                    " VALUES (?,'reserved','c',0,0)", ("o%d" % i,))
        inventory.reserve_optimistic(con, "o%d" % i, "S", 1, ttl=-1.0)
    for _ in range(4):
        with pytest.raises(RuntimeError):
            inventory.sweep_expired(con, crash_after=13)
    inventory.sweep_expired(con)
    r = con.execute("SELECT on_hand, reserved FROM stock WHERE sku='S'").fetchone()
    assert (r["on_hand"], r["reserved"]) == (100, 0)
    assert con.execute("SELECT COUNT(*) n FROM reservations WHERE state='held'"
                       ).fetchone()["n"] == 0
    assert inventory.check_invariants(con) == []


# --------------------------------------------------------------------------
# checkout
# --------------------------------------------------------------------------
def test_double_submit_creates_one_order(con):
    db.seed_stock(con, "S", 10, 1000)
    psp = FakePSP(seed=1)
    cart = [dict(sku="S", qty=1, unit_price=1000)]
    a = co.checkout(con, psp, "c", cart, idempotency_key="K")
    b = co.checkout(con, psp, "c", cart, idempotency_key="K")
    assert a["order_id"] == b["order_id"]
    assert b.get("replayed") is True
    assert con.execute("SELECT COUNT(*) n FROM orders").fetchone()["n"] == 1
    assert con.execute("SELECT COUNT(*) n FROM money_movements WHERE kind='capture'"
                       ).fetchone()["n"] == 1


def test_declined_payment_releases_the_reservation(con):
    db.seed_stock(con, "S", 10, 1000)
    psp = FakePSP(seed=2, decline_rate=1.0)
    r = co.checkout(con, psp, "c", [dict(sku="S", qty=3, unit_price=1000)],
                    idempotency_key="K")
    assert r["state"] == "payment_failed"
    st = con.execute("SELECT on_hand, reserved FROM stock WHERE sku='S'").fetchone()
    assert (st["on_hand"], st["reserved"]) == (10, 0)
    assert inventory.check_invariants(con) == []


def test_capture_unknown_is_reconciled_without_double_charging(con):
    db.seed_stock(con, "S", 100, 1000)
    psp = FakePSP(seed=3, timeout_rate=1.0)
    for i in range(10):
        r = co.checkout(con, psp, "c", [dict(sku="S", qty=1, unit_price=1000)],
                        idempotency_key="K%d" % i)
        assert r["state"] == "capture_unknown"

    stats = co.reconcile_capture_unknown(con, psp)
    assert stats["examined"] == 10 and stats["confirmed"] == 10

    # running again must change nothing
    again = co.reconcile_capture_unknown(con, psp)
    assert again == dict(examined=0, confirmed=0, voided=0)

    dupes = con.execute("SELECT order_id, COUNT(*) c FROM money_movements"
                        " WHERE kind='capture' GROUP BY order_id HAVING c>1").fetchall()
    assert dupes == []
    assert psp.captures_settled == 10
    assert oms.check_ledger_invariants(con) == []


def test_reservation_expiring_mid_payment_cannot_commit_stale_stock(con):
    """The reservation TTL elapsed and the sweeper released it while the PSP was
    thinking. The commit must not resurrect stock that is back on the shelf."""
    db.seed_stock(con, "S", 5, 1000)
    rid, _ = inventory.reserve_optimistic(con, "o", "S", 2, ttl=-1.0)
    inventory.sweep_expired(con)                       # released while paying
    assert inventory.commit_reservation(con, rid) is False
    r = con.execute("SELECT on_hand, reserved FROM stock WHERE sku='S'").fetchone()
    assert (r["on_hand"], r["reserved"]) == (5, 0)


# --------------------------------------------------------------------------
# the refund arithmetic
# --------------------------------------------------------------------------
def _discounted_order(con, psp, qtys=(1, 1, 1), pct=20):
    prices = [3000, 1500, 4500]
    for sku, p in zip("ABC", prices):
        db.seed_stock(con, sku, 100, p)
    gross = sum(q * p for q, p in zip(qtys, prices))
    disc = gross * pct // 100
    alloc = oms.allocate(disc, [q * p for q, p in zip(qtys, prices)])
    cart = [dict(sku=s, qty=q, unit_price=p, discount=a)
            for s, q, p, a in zip("ABC", qtys, prices, alloc)]
    r = co.checkout(con, psp, "c", cart, idempotency_key="K",
                    discount_total=disc, shipping=0)
    oid = r["order_id"]
    for s in ("allocated", "picked", "packed"):
        oms.transition(con, oid, s)
    oms.ship(con, oid, {i: q for i, q in enumerate(qtys)})
    oms.transition(con, oid, "delivered")
    oms.transition(con, oid, "return_requested")
    return oid


def test_partial_refund_returns_the_discounted_price_not_the_ticket_price(con):
    psp = FakePSP(seed=4)
    oid = _discounted_order(con, psp)
    q = oms.quote_return(con, oid, {2: 1})
    d = q["lines"][0]
    assert d["gross"] == 4500
    assert d["discount_returned"] == 900        # its share of the 20% off
    assert d["refund"] == 4500 - 900 + d["tax_returned"]
    assert d["refund"] < 4500                   # never the ticket price


@given(st.integers(1, 4), st.integers(1, 4), st.integers(1, 4),
       st.integers(0, 60))
@settings(max_examples=60, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_returning_everything_refunds_exactly_what_was_captured(qa, qb, qc, pct):
    """The invariant that makes the ledger trustworthy: unit-by-unit returns of
    the whole order must add up to the capture, to the cent."""
    path = os.path.join(tempfile.mkdtemp(), "t.db")
    c = db.init(path, fresh=True)
    try:
        psp = FakePSP(seed=6)
        oid = _discounted_order(c, psp, qtys=(qa, qb, qc), pct=pct)
        captured = oms.financial_summary(c, oid)["captured"]
        for line_no, qty in enumerate((qa, qb, qc)):
            for _ in range(qty):
                oms.process_return(c, psp, oid, {line_no: 1})
        s = oms.financial_summary(c, oid)
        assert s["refunded"] == captured
        assert s["net"] == 0
        assert s["invariant_ok"]
        assert oms.check_ledger_invariants(c) == []
    finally:
        c.close()


def test_cannot_refund_more_than_captured(con):
    psp = FakePSP(seed=8)
    oid = _discounted_order(con, psp)
    oms.process_return(con, psp, oid, {0: 1, 1: 1, 2: 1})
    with pytest.raises(oms.IllegalTransition):
        oms.process_return(con, psp, oid, {0: 1})     # nothing left to return
    assert oms.check_ledger_invariants(con) == []


def test_cannot_return_more_than_was_shipped(con):
    psp = FakePSP(seed=10)
    db.seed_stock(con, "A", 100, 3000)
    r = co.checkout(con, psp, "c", [dict(sku="A", qty=3, unit_price=3000)],
                    idempotency_key="K")
    oid = r["order_id"]
    for s in ("allocated", "picked", "packed"):
        oms.transition(con, oid, s)
    oms.ship(con, oid, {0: 2})                      # only 2 of 3 shipped
    with pytest.raises(oms.IllegalTransition):
        oms.quote_return(con, oid, {0: 3})


def test_cannot_ship_more_than_ordered(con):
    psp = FakePSP(seed=11)
    db.seed_stock(con, "A", 100, 3000)
    r = co.checkout(con, psp, "c", [dict(sku="A", qty=2, unit_price=3000)],
                    idempotency_key="K")
    oid = r["order_id"]
    for s in ("allocated", "picked", "packed"):
        oms.transition(con, oid, s)
    with pytest.raises(oms.IllegalTransition):
        oms.ship(con, oid, {0: 3})
