"""Order lifecycle: the unglamorous part, which is where the bugs and the money are.

Everyone builds the storefront. The part after the Buy button -- partial
shipments across warehouses, partial returns, proportional refunds, and a money
ledger that reconciles -- is where order-management systems actually fail.

TWO DESIGN COMMITMENTS
----------------------
1. The state machine is a TABLE, not a pile of if-statements. Illegal
   transitions are unreachable because `transition()` refuses them, not because
   no code path happens to call them today.

2. Money is APPEND-ONLY. Nothing updates or deletes a row in `money_movements`.
   An order's financial position is always recomputed from its movements, never
   stored and mutated, so the ledger and the summary cannot drift apart -- there
   is only one of them.

THE PROPORTIONAL REFUND
-----------------------
This is the case the spec says it will check, and it is where real OMS bugs
live. An order-level discount was ALLOCATED down to lines at checkout time (that
allocation is se2-promo-engine's job). Returning one unit of one line must
refund that unit's share of the line's discount, and returning every unit
one-at-a-time must refund exactly the line's whole discount -- no cent created,
none lost. Per-unit shares are apportioned by largest remainder for that reason;
naive division leaves cents stranded on the last unit returned.
"""
from __future__ import annotations

import sqlite3
import time

# --------------------------------------------------------------------------
# the state machine
# --------------------------------------------------------------------------
TRANSITIONS: dict[str, set[str]] = {
    "pending": {"reserved", "abandoned_out_of_stock", "payment_failed"},
    "reserved": {"placed", "capture_unknown", "payment_failed"},
    "capture_unknown": {"placed", "payment_failed"},
    "placed": {"allocated", "cancelled"},
    "allocated": {"picked", "cancelled"},
    "picked": {"packed"},
    "packed": {"partially_shipped", "shipped"},
    "partially_shipped": {"partially_shipped", "shipped"},
    "shipped": {"delivered"},
    "delivered": {"return_requested"},
    "return_requested": {"partially_returned", "returned"},
    "partially_returned": {"partially_returned", "returned", "return_requested"},
    "returned": set(),
    "cancelled": set(),
    "payment_failed": set(),
    "abandoned_out_of_stock": set(),
}

TERMINAL = {s for s, nxt in TRANSITIONS.items() if not nxt}


class IllegalTransition(Exception):
    pass


def transition(con: sqlite3.Connection, order_id: str, to: str, detail: str = "") -> None:
    row = con.execute("SELECT state FROM orders WHERE order_id=?", (order_id,)).fetchone()
    if row is None:
        raise IllegalTransition("no such order %s" % order_id)
    frm = row["state"]
    if to not in TRANSITIONS.get(frm, set()):
        raise IllegalTransition("%s -> %s is not a legal transition" % (frm, to))
    con.execute("UPDATE orders SET state=?, updated_at=? WHERE order_id=?",
                (to, time.time(), order_id))
    con.execute("INSERT INTO order_events(order_id,from_state,to_state,detail,"
                "created_at) VALUES (?,?,?,?,?)", (order_id, frm, to, detail, time.time()))


# --------------------------------------------------------------------------
# money apportionment
# --------------------------------------------------------------------------
def allocate(total: int, weights: list[int]) -> list[int]:
    """Largest-remainder split that sums EXACTLY to `total`. See se2's money.py."""
    if not weights:
        return []
    w_sum = sum(weights)
    if w_sum <= 0:
        out = [0] * len(weights)
        out[0] = total
        return out
    base, rem = [], []
    for i, w in enumerate(weights):
        q, r = divmod(total * w, w_sum)
        base.append(q)
        rem.append((r, -i))
    for _, negi in sorted(rem, reverse=True)[:total - sum(base)]:
        base[-negi] += 1
    return base


def line_unit_shares(line_discount: int, line_tax: int, qty: int):
    """Per-unit discount and tax shares that sum exactly to the line totals.

    Returned as lists indexed by unit, so returning units in ANY order and in any
    number of separate return events still adds up to the line's exact totals.
    Dividing and rounding per unit would strand cents on whichever unit came last.
    """
    ones = [1] * qty
    return allocate(line_discount, ones), allocate(line_tax, ones)


def order_tax_by_line(con, order_id: str) -> list[int]:
    """Apportion the order's tax across lines by post-discount line value."""
    o = con.execute("SELECT tax_total FROM orders WHERE order_id=?", (order_id,)).fetchone()
    lines = con.execute("SELECT line_no, qty, unit_price, discount FROM order_lines"
                        " WHERE order_id=? ORDER BY line_no", (order_id,)).fetchall()
    weights = [max(0, ln["qty"] * ln["unit_price"] - ln["discount"]) for ln in lines]
    return allocate(o["tax_total"], weights)


# --------------------------------------------------------------------------
# fulfilment
# --------------------------------------------------------------------------
def ship(con: sqlite3.Connection, order_id: str, shipments: dict[int, int],
         warehouse: str = "DC1") -> str:
    """Ship `{line_no: qty}`. Partial by design -- split across warehouses is the
    normal case, not the exception."""
    con.execute("BEGIN IMMEDIATE")
    try:
        for line_no, qty in shipments.items():
            r = con.execute("SELECT qty, qty_shipped FROM order_lines WHERE"
                            " order_id=? AND line_no=?", (order_id, line_no)).fetchone()
            if r is None:
                raise IllegalTransition("no line %d" % line_no)
            if qty <= 0 or r["qty_shipped"] + qty > r["qty"]:
                raise IllegalTransition(
                    "cannot ship %d of line %d (%d of %d already shipped)"
                    % (qty, line_no, r["qty_shipped"], r["qty"]))
            con.execute("UPDATE order_lines SET qty_shipped = qty_shipped + ?"
                        " WHERE order_id=? AND line_no=?", (qty, order_id, line_no))
        rows = con.execute("SELECT qty, qty_shipped FROM order_lines WHERE order_id=?",
                           (order_id,)).fetchall()
        fully = all(r["qty_shipped"] >= r["qty"] for r in rows)
        state = "shipped" if fully else "partially_shipped"
        transition(con, order_id, state, "warehouse=%s %s" % (warehouse, shipments))
        con.execute("COMMIT")
        return state
    except Exception:
        con.execute("ROLLBACK")
        raise


def quote_return(con: sqlite3.Connection, order_id: str,
                 returns: dict[int, int]) -> dict:
    """What a return is WORTH, computed before anything is mutated.

    Refund per returned unit = unit price
                             - that unit's allocated share of the line discount
                             + that unit's allocated share of the order tax

    Shipping is NOT refunded on a partial return. That is a policy decision, it
    belongs to the business, and it is written here so it can be pointed at --
    the failure mode in real systems is that nobody decided and different code
    paths answer differently.
    """
    tax_by_line = order_tax_by_line(con, order_id)
    lines = con.execute("SELECT line_no, sku, qty, unit_price, discount, qty_shipped,"
                        " qty_returned FROM order_lines WHERE order_id=? ORDER BY line_no",
                        (order_id,)).fetchall()
    detail, total = [], 0
    for ln in lines:
        want = returns.get(ln["line_no"], 0)
        if want <= 0:
            continue
        already = ln["qty_returned"]
        if want + already > ln["qty_shipped"]:
            raise IllegalTransition(
                "cannot return %d of line %d: %d shipped, %d already returned"
                % (want, ln["line_no"], ln["qty_shipped"], already))
        disc_units, tax_units = line_unit_shares(
            ln["discount"], tax_by_line[ln["line_no"]], ln["qty"])
        # units [already, already+want) are the ones coming back
        idx = range(already, already + want)
        gross = want * ln["unit_price"]
        disc = sum(disc_units[i] for i in idx)
        tax = sum(tax_units[i] for i in idx)
        amount = gross - disc + tax
        total += amount
        detail.append(dict(line_no=ln["line_no"], sku=ln["sku"], qty=want,
                           gross=gross, discount_returned=disc, tax_returned=tax,
                           refund=amount))
    return dict(order_id=order_id, total=total, lines=detail)


def process_return(con: sqlite3.Connection, psp, order_id: str,
                   returns: dict[int, int], restock: bool = True) -> dict:
    """Quote, refund, record the movement, restock. One transaction."""
    quote = quote_return(con, order_id, returns)
    con.execute("BEGIN IMMEDIATE")
    try:
        summary = financial_summary(con, order_id)
        if quote["total"] > summary["refundable"]:
            raise IllegalTransition(
                "refund %d exceeds refundable %d" % (quote["total"], summary["refundable"]))

        o = con.execute("SELECT psp_ref FROM orders WHERE order_id=?", (order_id,)).fetchone()
        psp_ref = psp.refund(o["psp_ref"], quote["total"]) if quote["total"] > 0 else None

        for d in quote["lines"]:
            con.execute("UPDATE order_lines SET qty_returned = qty_returned + ?"
                        " WHERE order_id=? AND line_no=?",
                        (d["qty"], order_id, d["line_no"]))
            if restock:
                con.execute("UPDATE stock SET on_hand = on_hand + ?, version = version + 1"
                            " WHERE sku=?", (d["qty"], d["sku"]))
        if quote["total"] > 0:
            con.execute("INSERT INTO money_movements(order_id,kind,amount,reason,"
                        "psp_ref,created_at) VALUES (?, 'refund', ?, ?, ?, ?)",
                        (order_id, quote["total"], "return", psp_ref, time.time()))

        rows = con.execute("SELECT qty, qty_returned FROM order_lines WHERE order_id=?",
                           (order_id,)).fetchall()
        fully = all(r["qty_returned"] >= r["qty"] for r in rows)
        transition(con, order_id, "returned" if fully else "partially_returned",
                   "refund=%d" % quote["total"])
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return quote


# --------------------------------------------------------------------------
# the ledger
# --------------------------------------------------------------------------
def financial_summary(con: sqlite3.Connection, order_id: str) -> dict:
    """Reconstructed from movements alone. Nothing here is a stored balance.

    The per-order invariant is `refunded <= captured`, always. It is checked here
    on every read and asserted in the test suite, because an order that has
    refunded more than it captured is money leaving the business through a bug.
    """
    rows = con.execute("SELECT kind, amount FROM money_movements WHERE order_id=?",
                       (order_id,)).fetchall()
    captured = sum(r["amount"] for r in rows if r["kind"] == "capture")
    refunded = sum(r["amount"] for r in rows if r["kind"] == "refund")
    adjust = sum(r["amount"] for r in rows if r["kind"] == "adjustment")
    return dict(order_id=order_id, captured=captured, refunded=refunded,
                adjustments=adjust, net=captured - refunded,
                refundable=captured - refunded,
                invariant_ok=refunded <= captured)


def check_ledger_invariants(con: sqlite3.Connection) -> list[str]:
    problems = []
    for r in con.execute("SELECT DISTINCT order_id FROM money_movements"):
        s = financial_summary(con, r["order_id"])
        if not s["invariant_ok"]:
            problems.append("order %s refunded %d > captured %d"
                            % (s["order_id"], s["refunded"], s["captured"]))
        if s["net"] < 0:
            problems.append("order %s has negative net %d" % (s["order_id"], s["net"]))
    for r in con.execute("SELECT order_id, line_no, qty, qty_shipped, qty_returned"
                         " FROM order_lines"):
        if r["qty_shipped"] > r["qty"]:
            problems.append("line %s/%d shipped %d of %d"
                            % (r["order_id"], r["line_no"], r["qty_shipped"], r["qty"]))
        if r["qty_returned"] > r["qty_shipped"]:
            problems.append("line %s/%d returned %d but only shipped %d"
                            % (r["order_id"], r["line_no"], r["qty_returned"],
                               r["qty_shipped"]))
    return problems


def audit_trail(con: sqlite3.Connection, order_id: str) -> list[str]:
    """The CS-agent view: what happened to this order, in order."""
    out = []
    for e in con.execute("SELECT from_state,to_state,detail,created_at FROM"
                         " order_events WHERE order_id=? ORDER BY event_id",
                         (order_id,)):
        out.append("  %-22s -> %-22s %s" % (e["from_state"] or "-", e["to_state"],
                                            e["detail"] or ""))
    for m in con.execute("SELECT kind,amount,reason,created_at FROM money_movements"
                         " WHERE order_id=? ORDER BY movement_id", (order_id,)):
        out.append("  MONEY %-8s %8d  %s" % (m["kind"], m["amount"], m["reason"]))
    return out
