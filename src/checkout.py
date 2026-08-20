"""Checkout orchestration: validate -> price -> reserve -> capture -> confirm.

Every step can fail, and the interesting engineering is entirely in the failure
handling. Three cases are implemented rather than described:

  1. IDEMPOTENT RETRY. A client that resends the same idempotency key gets the
     original outcome, not a second order. Enforced by a UNIQUE constraint, not
     by a check-then-insert -- the check-then-insert version loses the race it
     exists to prevent.

  2. PSP TIMEOUT AFTER CAPTURE. The order goes to `capture_unknown` and stays
     there. A reconciliation job asks the PSP what actually happened and either
     confirms the order or voids and releases the stock. The customer is never
     charged twice and a paid order is never silently dropped.

  3. RESERVATION EXPIRY MID-PAYMENT. The reservation TTL can elapse while the
     PSP is thinking. The commit is guarded on the reservation still being
     'held', so a resurrected payment cannot commit stock that was already
     returned to the shelf.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid

from . import inventory
from .psp import FakePSP, PSPDeclined, PSPTimeout

TAX_BP = 875  # 8.75%, flat -- real tax is a nexus problem, see README


class CheckoutError(Exception):
    pass


def _event(con, order_id, frm, to, detail=""):
    con.execute("INSERT INTO order_events(order_id,from_state,to_state,detail,"
                "created_at) VALUES (?,?,?,?,?)",
                (order_id, frm, to, detail, time.time()))


def _set_state(con, order_id, to, detail=""):
    row = con.execute("SELECT state FROM orders WHERE order_id=?", (order_id,)).fetchone()
    frm = row["state"] if row else None
    con.execute("UPDATE orders SET state=?, updated_at=? WHERE order_id=?",
                (to, time.time(), order_id))
    _event(con, order_id, frm, to, detail)


def price_cart(lines, discount_total: int = 0, shipping: int = 0):
    """items + promos + shipping + tax.

    `discount_total` arrives already ALLOCATED per line, which is what
    se2-promo-engine produces. That allocation is not cosmetic -- without it the
    partial-refund arithmetic in oms.py has nothing to work from.
    """
    merch = sum(ln["qty"] * ln["unit_price"] for ln in lines)
    taxable = merch - discount_total + shipping
    tax = (taxable * TAX_BP + 5000) // 10000
    return dict(merch_total=merch, discount_total=discount_total,
                shipping_total=shipping, tax_total=tax,
                grand_total=taxable + tax)


def checkout(con: sqlite3.Connection, psp: FakePSP, customer_id: str, lines: list,
             idempotency_key: str, mechanism: str = "optimistic",
             shipping: int = 0, discount_total: int = 0,
             reservation_ttl: float = inventory.RESERVATION_TTL_SECONDS) -> dict:
    """Returns {order_id, state, ...}. Safe to call repeatedly with one key."""

    # ---- 1. idempotency, enforced by the database ----------------------
    existing = con.execute(
        "SELECT order_id, state FROM orders WHERE idempotency_key=?",
        (idempotency_key,)).fetchone()
    if existing:
        return dict(order_id=existing["order_id"], state=existing["state"],
                    replayed=True)

    order_id = "ord_" + uuid.uuid4().hex[:16]
    priced = price_cart(lines, discount_total, shipping)
    now = time.time()
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            "INSERT INTO orders(order_id,state,idempotency_key,customer_id,"
            "merch_total,discount_total,shipping_total,tax_total,grand_total,"
            "created_at,updated_at) VALUES (?,'pending',?,?,?,?,?,?,?,?,?)",
            (order_id, idempotency_key, customer_id, priced["merch_total"],
             priced["discount_total"], priced["shipping_total"],
             priced["tax_total"], priced["grand_total"], now, now))
        for i, ln in enumerate(lines):
            con.execute(
                "INSERT INTO order_lines(order_id,line_no,sku,qty,unit_price,discount)"
                " VALUES (?,?,?,?,?,?)",
                (order_id, i, ln["sku"], ln["qty"], ln["unit_price"],
                 ln.get("discount", 0)))
        _event(con, order_id, None, "pending", "checkout started")
        con.execute("COMMIT")
    except sqlite3.IntegrityError:
        # Lost the idempotency race to a concurrent identical request. The
        # UNIQUE constraint is the arbiter; we read back the winner's order.
        con.execute("ROLLBACK")
        row = con.execute("SELECT order_id, state FROM orders WHERE idempotency_key=?",
                          (idempotency_key,)).fetchone()
        return dict(order_id=row["order_id"], state=row["state"], replayed=True)
    except Exception:
        con.execute("ROLLBACK")
        raise

    # ---- 2. reserve ----------------------------------------------------
    reserve = (inventory.reserve_optimistic if mechanism == "optimistic"
               else inventory.reserve_pessimistic)
    res_ids, attempts_total = [], 0
    try:
        for ln in lines:
            rid, attempts = reserve(con, order_id, ln["sku"], ln["qty"],
                                    ttl=reservation_ttl)
            res_ids.append(rid)
            attempts_total += attempts
    except inventory.OutOfStock as e:
        for rid in res_ids:
            con.execute("BEGIN IMMEDIATE")
            inventory.release_reservation(con, rid)
            con.execute("COMMIT")
        _set_state(con, order_id, "abandoned_out_of_stock", str(e))
        return dict(order_id=order_id, state="abandoned_out_of_stock",
                    reason=str(e), attempts=attempts_total)

    _set_state(con, order_id, "reserved", json.dumps({"reservations": len(res_ids)}))

    # ---- 3. capture ----------------------------------------------------
    attempt_id = "att_" + uuid.uuid4().hex[:12]
    # The idempotency key sent to the PSP is derived from OUR attempt id, so the
    # reconciliation job below can search for it. Capturing without a key you can
    # later look up makes the ambiguous case unresolvable.
    psp_idem = "%s:%s" % (order_id, attempt_id)
    con.execute("INSERT INTO payment_attempts(attempt_id,order_id,amount,state,"
                "created_at,updated_at) VALUES (?,?,?,'pending',?,?)",
                (attempt_id, order_id, priced["grand_total"], time.time(), time.time()))
    try:
        psp_ref = psp.capture(order_id, priced["grand_total"], psp_idem)
    except PSPTimeout:
        con.execute("UPDATE payment_attempts SET state='capture_unknown',updated_at=?"
                    " WHERE attempt_id=?", (time.time(), attempt_id))
        _set_state(con, order_id, "capture_unknown",
                   "PSP did not respond; funds may or may not have moved")
        return dict(order_id=order_id, state="capture_unknown",
                    attempts=attempts_total)
    except PSPDeclined as e:
        con.execute("UPDATE payment_attempts SET state='failed',updated_at=?"
                    " WHERE attempt_id=?", (time.time(), attempt_id))
        for rid in res_ids:
            con.execute("BEGIN IMMEDIATE")
            inventory.release_reservation(con, rid)
            con.execute("COMMIT")
        _set_state(con, order_id, "payment_failed", str(e))
        return dict(order_id=order_id, state="payment_failed", reason=str(e))

    # ---- 4. confirm ----------------------------------------------------
    return _confirm(con, order_id, attempt_id, psp_ref, priced["grand_total"],
                    attempts_total)


def _confirm(con, order_id, attempt_id, psp_ref, amount, attempts_total=0):
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute("UPDATE payment_attempts SET state='captured',psp_ref=?,"
                    "updated_at=? WHERE attempt_id=?",
                    (psp_ref, time.time(), attempt_id))
        con.execute("INSERT INTO money_movements(order_id,kind,amount,reason,"
                    "psp_ref,created_at) VALUES (?, 'capture', ?, 'checkout', ?, ?)",
                    (order_id, amount, psp_ref, time.time()))
        con.execute("UPDATE orders SET psp_ref=? WHERE order_id=?", (psp_ref, order_id))
        committed = 0
        for r in con.execute("SELECT reservation_id FROM reservations WHERE"
                             " order_id=? AND state='held'", (order_id,)).fetchall():
            if inventory.commit_reservation(con, r["reservation_id"]):
                committed += 1
        _set_state(con, order_id, "placed", "captured %d" % amount)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return dict(order_id=order_id, state="placed", psp_ref=psp_ref,
                attempts=attempts_total, reservations_committed=committed)


# --------------------------------------------------------------------------
def reconcile_capture_unknown(con: sqlite3.Connection, psp: FakePSP) -> dict:
    """Resolve every ambiguous capture by asking the PSP what really happened.

    This job is the entire answer to "the PSP timed out after you sent the
    capture". It is idempotent and safe to run on a schedule: an order it has
    already resolved is no longer in `capture_unknown` and is skipped.

    Two outcomes, both terminal:
      - the PSP has the money  -> confirm the order, commit stock, record the
                                  capture movement. The customer is charged once.
      - the PSP has nothing    -> void, release the stock, fail the order.
    """
    stats = dict(examined=0, confirmed=0, voided=0)
    rows = con.execute(
        "SELECT pa.attempt_id, pa.order_id, pa.amount FROM payment_attempts pa "
        "JOIN orders o ON o.order_id = pa.order_id "
        "WHERE pa.state='capture_unknown' AND o.state='capture_unknown'").fetchall()

    for r in rows:
        stats["examined"] += 1
        psp_idem = "%s:%s" % (r["order_id"], r["attempt_id"])
        rec = psp.lookup_by_idempotency(psp_idem)

        if rec and rec["state"] == "captured":
            _confirm(con, r["order_id"], r["attempt_id"], rec["psp_ref"], r["amount"])
            stats["confirmed"] += 1
        else:
            con.execute("BEGIN IMMEDIATE")
            try:
                if rec:
                    psp.void(rec["psp_ref"])
                con.execute("UPDATE payment_attempts SET state='voided',updated_at=?"
                            " WHERE attempt_id=?", (time.time(), r["attempt_id"]))
                for res in con.execute("SELECT reservation_id FROM reservations"
                                       " WHERE order_id=? AND state='held'",
                                       (r["order_id"],)).fetchall():
                    inventory.release_reservation(con, res["reservation_id"])
                _set_state(con, r["order_id"], "payment_failed",
                           "reconciliation: PSP had no capture")
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
            stats["voided"] += 1
    return stats
