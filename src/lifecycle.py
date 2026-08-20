"""Exchanges, repricing, and the operational metrics the event log was for.

Three things the first pass named as missing, and each one is a place where a
"simple" answer is quietly wrong.

EXCHANGES ARE NOT RETURN-PLUS-NEW-ORDER
---------------------------------------
The tempting implementation is to refund the returned item and let the customer
place a new order. It is wrong in three ways a customer notices:

  - the replacement is not RESERVED while the return is in flight, so the size
    they wanted sells out between the refund and the reorder
  - the refund and the new charge are two money movements, so the customer sees
    a debit before the credit clears and phones support about being charged twice
  - the price may have moved, so an even exchange becomes an argument

So an exchange is modelled as a LINKED ORDER: the replacement order is created
and reserved immediately, priced at the ORIGINAL order's price, and the money is
settled as a single net adjustment rather than a refund plus a capture.

REPRICING
---------
Prices move between the cart being built and checkout being submitted. The
policy question is not "does the price change" but "who absorbs it, and up to
what threshold". Silently charging the new higher price is the failure that
generates chargebacks; silently honouring a stale price forever is the failure
that lets a pricing error become a promotion. Both bounds are configurable and
the decision is recorded on the order.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid

from . import inventory, oms

SCHEMA = """
CREATE TABLE IF NOT EXISTS order_links (
    parent_order_id TEXT NOT NULL,
    child_order_id  TEXT NOT NULL,
    link_type       TEXT NOT NULL CHECK (link_type IN ('exchange','replacement')),
    created_at      REAL NOT NULL,
    PRIMARY KEY (parent_order_id, child_order_id)
);

CREATE TABLE IF NOT EXISTS price_quotes (
    order_id    TEXT NOT NULL,
    line_no     INTEGER NOT NULL,
    quoted_at   REAL NOT NULL,
    quoted_price INTEGER NOT NULL,
    PRIMARY KEY (order_id, line_no)
);
"""

# Repricing policy. Named constants because they ARE the policy -- a merchant
# argues about these numbers, not about the code.
HONOUR_INCREASE_UP_TO_CENTS = 300     # absorb small rises rather than re-prompt
HONOUR_INCREASE_UP_TO_BP = 500        # ...or 5%, whichever is smaller
ALWAYS_PASS_DECREASES = True          # a price drop is always given to the customer


def init(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)


# --------------------------------------------------------------------------
# repricing
# --------------------------------------------------------------------------
def quote(con, order_id: str, line_no: int, price: int, now: float | None = None):
    con.execute("INSERT OR REPLACE INTO price_quotes VALUES (?,?,?,?)",
                (order_id, line_no, time.time() if now is None else now, price))


def reprice(con: sqlite3.Connection, order_id: str,
            current_prices: dict[int, int]) -> dict:
    """Compare quoted prices to current ones and apply the policy.

    Returns a decision per line plus an overall verdict:
      'unchanged'      nothing moved
      'honoured'       price rose within tolerance; we absorb it
      'reduced'        price fell; the customer gets it
      'reconfirm'      price rose beyond tolerance; the customer must re-approve

    'reconfirm' is a REAL outcome, not an error. The alternative -- charging more
    than the customer agreed to -- is the one that generates chargebacks, and a
    chargeback costs more than the abandoned cart.
    """
    rows = con.execute("SELECT line_no, quoted_price FROM price_quotes"
                       " WHERE order_id=? ORDER BY line_no", (order_id,)).fetchall()
    decisions, delta = [], 0
    verdict = "unchanged"
    for r in rows:
        ln, quoted = r["line_no"], r["quoted_price"]
        current = current_prices.get(ln, quoted)
        if current == quoted:
            decisions.append(dict(line_no=ln, quoted=quoted, current=current,
                                  decision="unchanged", charged=quoted))
            continue
        if current < quoted:
            charged = current if ALWAYS_PASS_DECREASES else quoted
            decisions.append(dict(line_no=ln, quoted=quoted, current=current,
                                  decision="reduced", charged=charged))
            delta += charged - quoted
            verdict = "reduced" if verdict == "unchanged" else verdict
            continue
        rise = current - quoted
        tolerance = min(HONOUR_INCREASE_UP_TO_CENTS,
                        (quoted * HONOUR_INCREASE_UP_TO_BP) // 10_000)
        if rise <= tolerance:
            decisions.append(dict(line_no=ln, quoted=quoted, current=current,
                                  decision="honoured", charged=quoted))
            verdict = "honoured" if verdict in ("unchanged", "reduced") else verdict
        else:
            decisions.append(dict(line_no=ln, quoted=quoted, current=current,
                                  decision="reconfirm", charged=None))
            verdict = "reconfirm"
    con.execute("INSERT INTO order_events(order_id,from_state,to_state,detail,"
                "created_at) VALUES (?,NULL,'repriced',?,?)",
                (order_id, json.dumps(dict(verdict=verdict, delta=delta)), time.time()))
    return dict(verdict=verdict, delta_cents=delta, lines=decisions)


# --------------------------------------------------------------------------
# exchanges
# --------------------------------------------------------------------------
def create_exchange(con: sqlite3.Connection, psp, parent_order_id: str,
                    returns: dict[int, int], replacements: list[dict],
                    mechanism: str = "optimistic") -> dict:
    """Return some units and ship different ones, as ONE linked transaction.

    `replacements` is a list of {sku, qty, unit_price}. The replacement is
    reserved BEFORE the return is processed -- if the size the customer wants is
    gone, they find out now rather than after their refund has been issued and
    their original item is in the post.

    Money settles as a single NET movement: a refund if the exchange is
    downward, an additional capture if upward, and nothing at all if even. That
    is the whole reason not to model this as refund-then-reorder.
    """
    quote_ = oms.quote_return(con, parent_order_id, returns)
    child_id = "ord_" + uuid.uuid4().hex[:16]
    now = time.time()

    # ---- reserve the replacement FIRST ----
    reserve = (inventory.reserve_optimistic if mechanism == "optimistic"
               else inventory.reserve_pessimistic)
    res_ids = []
    try:
        for rep in replacements:
            rid, _ = reserve(con, child_id, rep["sku"], rep["qty"])
            res_ids.append(rid)
    except inventory.OutOfStock as e:
        for rid in res_ids:
            con.execute("BEGIN IMMEDIATE")
            inventory.release_reservation(con, rid)
            con.execute("COMMIT")
        return dict(ok=False, reason="replacement_out_of_stock", detail=str(e))

    # The replacement has to carry TAX, because the return quote refunds tax.
    # Comparing a tax-inclusive refund to a tax-exclusive replacement made a
    # like-for-like swap net the tax amount -- the report claimed "an even
    # exchange moves no money" while its own output showed a $2.63 refund.
    # An even swap must net exactly zero, and that is now asserted in a test.
    from .checkout import TAX_BP
    replacement_merch = sum(r["qty"] * r["unit_price"] for r in replacements)
    replacement_tax = (replacement_merch * TAX_BP + 5000) // 10000
    replacement_value = replacement_merch + replacement_tax
    net = replacement_value - quote_["total"]

    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute(
            "INSERT INTO orders(order_id,state,idempotency_key,customer_id,"
            "merch_total,discount_total,shipping_total,tax_total,grand_total,"
            "created_at,updated_at) SELECT ?,'placed',?,customer_id,?,0,0,0,?,?,?"
            " FROM orders WHERE order_id=?",
            (child_id, "exchange:" + child_id, replacement_merch,
             max(net, 0), now, now, parent_order_id))
        for i, rep in enumerate(replacements):
            con.execute("INSERT INTO order_lines(order_id,line_no,sku,qty,"
                        "unit_price,discount) VALUES (?,?,?,?,?,0)",
                        (child_id, i, rep["sku"], rep["qty"], rep["unit_price"]))
        con.execute("INSERT INTO order_links VALUES (?,?, 'exchange', ?)",
                    (parent_order_id, child_id, now))

        for rid in res_ids:
            inventory.commit_reservation(con, rid)

        # mark the returned units on the PARENT
        for d in quote_["lines"]:
            con.execute("UPDATE order_lines SET qty_returned = qty_returned + ?"
                        " WHERE order_id=? AND line_no=?",
                        (d["qty"], parent_order_id, d["line_no"]))
            con.execute("UPDATE stock SET on_hand = on_hand + ?, version = version + 1"
                        " WHERE sku=?", (d["qty"], d["sku"]))

        # ---- ONE net money movement ----
        o = con.execute("SELECT psp_ref FROM orders WHERE order_id=?",
                        (parent_order_id,)).fetchone()
        if net < 0:
            psp_ref = psp.refund(o["psp_ref"], -net)
            con.execute("INSERT INTO money_movements(order_id,kind,amount,reason,"
                        "psp_ref,created_at) VALUES (?, 'refund', ?, ?, ?, ?)",
                        (parent_order_id, -net, "exchange_net", psp_ref, now))
        elif net > 0:
            psp_ref = psp.capture(child_id, net, "exchange:" + child_id)
            con.execute("INSERT INTO money_movements(order_id,kind,amount,reason,"
                        "psp_ref,created_at) VALUES (?, 'capture', ?, ?, ?, ?)",
                        (child_id, net, "exchange_net", psp_ref, now))

        rows = con.execute("SELECT qty, qty_returned FROM order_lines"
                           " WHERE order_id=?", (parent_order_id,)).fetchall()
        fully = all(r["qty_returned"] >= r["qty"] for r in rows)
        oms.transition(con, parent_order_id,
                       "returned" if fully else "partially_returned",
                       "exchange -> %s net %d" % (child_id, net))
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise

    return dict(ok=True, child_order_id=child_id, returned_value=quote_["total"],
                replacement_value=replacement_value,
                replacement_merch=replacement_merch,
                replacement_tax=replacement_tax, net_cents=net,
                money_movements=1 if net != 0 else 0)


# --------------------------------------------------------------------------
# ops metrics -- what the event log was always for
# --------------------------------------------------------------------------
def funnel(con: sqlite3.Connection) -> list[dict]:
    """Checkout conversion by step, reconstructed from order_events.

    The event log existed from the first commit and nothing read it except the
    CS-agent audit view. This is the other consumer, and it is the reason to
    write transitions to a log rather than only mutating a state column: the
    funnel is a QUERY over history, not a set of counters someone remembered to
    increment.
    """
    total = con.execute("SELECT COUNT(*) n FROM orders").fetchone()["n"]
    steps = [
        ("started", "SELECT COUNT(DISTINCT order_id) n FROM order_events"
                    " WHERE to_state='pending'"),
        ("reserved", "SELECT COUNT(DISTINCT order_id) n FROM order_events"
                     " WHERE to_state='reserved'"),
        ("placed", "SELECT COUNT(DISTINCT order_id) n FROM order_events"
                   " WHERE to_state='placed'"),
        ("shipped", "SELECT COUNT(DISTINCT order_id) n FROM order_events"
                    " WHERE to_state IN ('shipped','partially_shipped')"),
        ("delivered", "SELECT COUNT(DISTINCT order_id) n FROM order_events"
                      " WHERE to_state='delivered'"),
    ]
    out, prev = [], None
    for name, q in steps:
        n = con.execute(q).fetchone()["n"]
        out.append(dict(step=name, orders=n,
                        pct_of_started=(100.0 * n / out[0]["orders"]) if out else 100.0,
                        step_conversion=(100.0 * n / prev) if prev else 100.0))
        prev = n if n else None
    return out


def health(con: sqlite3.Connection) -> dict:
    """The three numbers an on-call engineer looks at first."""
    def one(q):
        return con.execute(q).fetchone()["n"]

    orders = one("SELECT COUNT(*) n FROM orders")
    held = one("SELECT COUNT(*) n FROM reservations WHERE state='held'")
    expired = one("SELECT COUNT(*) n FROM reservations WHERE state='released'")
    committed = one("SELECT COUNT(*) n FROM reservations WHERE state='committed'")
    unknown = one("SELECT COUNT(*) n FROM orders WHERE state='capture_unknown'")
    failed = one("SELECT COUNT(*) n FROM orders WHERE state='payment_failed'")
    oos = one("SELECT COUNT(*) n FROM orders WHERE state='abandoned_out_of_stock'")
    res_total = held + expired + committed
    return dict(
        orders=orders,
        reservation_expiry_rate=(expired / res_total) if res_total else 0.0,
        capture_unknown_open=unknown,
        payment_failure_rate=(failed / orders) if orders else 0.0,
        out_of_stock_rate=(oos / orders) if orders else 0.0,
        reservations_held_now=held)
