"""Reservation model and the two concurrency mechanisms.

WHY ADD-TO-CART DOES NOT RESERVE
--------------------------------
Cart abandonment runs 65-80% in real e-commerce. If adding to cart reserved
stock, then for every unit sold you would take four-plus units off the shelf and
hold them for however long your cart TTL is -- a self-inflicted stockout machine
that reports healthy on-hand and cannot fulfil anything. Reservation starts at
CHECKOUT-START, where intent is demonstrated and the population is small enough
that a 10-minute hold is cheap.

The exception is scarcity economics: Ticketmaster reserves at seat-select
because the inventory is unique, non-substitutable, and the entire product is
the queue. Retail stock is fungible and the abandonment rate is the dominant
term. Same mechanism, opposite decision, because the economics differ.

THE TWO MECHANISMS
------------------
Optimistic (compare-and-set): a conditional UPDATE guarded on a version column.
No lock is taken; the loser of a race sees rowcount 0 and retries or fails clean.
Cheap when contention is low, degrades into retry storms when it is high.

Pessimistic: BEGIN IMMEDIATE takes the write lock up front, so the read and the
write are one critical section. Predictable under contention, serialising when
there is none.

Both are implemented so the choice can be DEFENDED with a measurement rather than
asserted. See run_flashsale.py.
"""
from __future__ import annotations

import sqlite3
import time
import uuid

RESERVATION_TTL_SECONDS = 600.0  # 10 minutes


class OutOfStock(Exception):
    pass


# --------------------------------------------------------------------------
def reserve_optimistic(con: sqlite3.Connection, order_id: str, sku: str, qty: int,
                       ttl: float = RESERVATION_TTL_SECONDS,
                       max_retries: int = 8) -> tuple[str, int]:
    """Compare-and-set on (sku, version). Returns (reservation_id, attempts).

    The UPDATE carries its own precondition: the version must be unchanged AND
    there must be enough free stock. A caller that loses the race changes
    nothing, sees rowcount 0, re-reads and tries again. There is no window in
    which two callers can both believe they hold the last unit, because the
    check and the decrement are the same statement.
    """
    for attempt in range(1, max_retries + 1):
        row = con.execute("SELECT on_hand, reserved, version FROM stock WHERE sku=?",
                          (sku,)).fetchone()
        if row is None:
            raise OutOfStock("unknown sku %s" % sku)
        free = row["on_hand"] - row["reserved"]
        if free < qty:
            raise OutOfStock("sold out")

        cur = con.execute(
            "UPDATE stock SET reserved = reserved + ?, version = version + 1 "
            "WHERE sku = ? AND version = ? AND on_hand - reserved >= ?",
            (qty, sku, row["version"], qty))
        if cur.rowcount == 1:
            rid = uuid.uuid4().hex
            now = time.time()
            con.execute(
                "INSERT INTO reservations(reservation_id,order_id,sku,qty,state,"
                "expires_at,created_at) VALUES (?,?,?,?, 'held', ?, ?)",
                (rid, order_id, sku, qty, now + ttl, now))
            return rid, attempt
        # lost the race; loop and re-read
    raise OutOfStock("contention: exhausted %d retries" % max_retries)


def reserve_pessimistic(con: sqlite3.Connection, order_id: str, sku: str, qty: int,
                        ttl: float = RESERVATION_TTL_SECONDS) -> tuple[str, int]:
    """BEGIN IMMEDIATE: acquire the write lock before reading.

    SQLite has no SELECT ... FOR UPDATE; BEGIN IMMEDIATE is its equivalent, and
    it takes the database write lock for the whole transaction. On Postgres this
    would be a row lock and the difference matters -- see the README.
    """
    con.execute("BEGIN IMMEDIATE")
    try:
        row = con.execute("SELECT on_hand, reserved FROM stock WHERE sku=?",
                          (sku,)).fetchone()
        if row is None:
            raise OutOfStock("unknown sku %s" % sku)
        if row["on_hand"] - row["reserved"] < qty:
            raise OutOfStock("sold out")
        con.execute("UPDATE stock SET reserved = reserved + ?, version = version + 1"
                    " WHERE sku = ?", (qty, sku))
        rid = uuid.uuid4().hex
        now = time.time()
        con.execute(
            "INSERT INTO reservations(reservation_id,order_id,sku,qty,state,"
            "expires_at,created_at) VALUES (?,?,?,?, 'held', ?, ?)",
            (rid, order_id, sku, qty, now + ttl, now))
        con.execute("COMMIT")
        return rid, 1
    except Exception:
        con.execute("ROLLBACK")
        raise


# --------------------------------------------------------------------------
def commit_reservation(con: sqlite3.Connection, reservation_id: str) -> bool:
    """held -> committed: the units leave on_hand for good. Idempotent.

    Guarded on state='held' in the WHERE clause, so a second call changes nothing
    and returns False rather than decrementing stock twice.
    """
    cur = con.execute("UPDATE reservations SET state='committed' "
                      "WHERE reservation_id=? AND state='held'", (reservation_id,))
    if cur.rowcount != 1:
        return False
    r = con.execute("SELECT sku, qty FROM reservations WHERE reservation_id=?",
                    (reservation_id,)).fetchone()
    con.execute("UPDATE stock SET on_hand = on_hand - ?, reserved = reserved - ?,"
                " version = version + 1 WHERE sku = ?", (r["qty"], r["qty"], r["sku"]))
    return True


def release_reservation(con: sqlite3.Connection, reservation_id: str) -> bool:
    """held -> released: the units go back on the shelf. Idempotent.

    The state guard is what makes the expiry sweeper crash-safe. A reservation
    can be released exactly once no matter how many times the sweeper is
    interrupted and restarted, because the UPDATE that frees the stock and the
    UPDATE that marks it released are the same transaction and both are
    predicated on the row still being 'held'.
    """
    cur = con.execute("UPDATE reservations SET state='released' "
                      "WHERE reservation_id=? AND state='held'", (reservation_id,))
    if cur.rowcount != 1:
        return False
    r = con.execute("SELECT sku, qty FROM reservations WHERE reservation_id=?",
                    (reservation_id,)).fetchone()
    con.execute("UPDATE stock SET reserved = reserved - ?, version = version + 1"
                " WHERE sku = ?", (r["qty"], r["sku"]))
    return True


def sweep_expired(con: sqlite3.Connection, now: float | None = None,
                  limit: int = 10_000, crash_after: int | None = None) -> int:
    """Release every reservation past its TTL. Idempotent and crash-safe.

    `crash_after` exists for the chaos drill: it raises mid-sweep so the test can
    assert what is guaranteed on restart. Each release is its own transaction, so
    a crash leaves a prefix of the batch released and the rest still 'held' --
    never a half-released reservation whose stock was returned twice or not at all.
    """
    now = time.time() if now is None else now
    rows = con.execute(
        "SELECT reservation_id FROM reservations WHERE state='held' AND expires_at <= ?"
        " ORDER BY expires_at LIMIT ?", (now, limit)).fetchall()
    n = 0
    for i, r in enumerate(rows):
        if crash_after is not None and i == crash_after:
            raise RuntimeError("sweeper crashed after %d releases" % n)
        con.execute("BEGIN IMMEDIATE")
        try:
            ok = release_reservation(con, r["reservation_id"])
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        n += 1 if ok else 0
    return n


# --------------------------------------------------------------------------
def check_invariants(con: sqlite3.Connection) -> list[str]:
    """Cheap assertions that should hold after any sequence of operations."""
    problems = []
    for r in con.execute("SELECT sku, on_hand, reserved FROM stock"):
        if r["on_hand"] < 0:
            problems.append("negative on_hand for %s: %d" % (r["sku"], r["on_hand"]))
        if r["reserved"] < 0:
            problems.append("negative reserved for %s: %d" % (r["sku"], r["reserved"]))
        if r["reserved"] > r["on_hand"]:
            problems.append("oversold %s: reserved %d > on_hand %d"
                            % (r["sku"], r["reserved"], r["on_hand"]))
    # held reservations must be exactly accounted for in stock.reserved
    for r in con.execute(
            "SELECT s.sku, s.reserved, COALESCE(SUM(CASE WHEN r.state='held' "
            "THEN r.qty END),0) AS held FROM stock s LEFT JOIN reservations r "
            "ON r.sku=s.sku GROUP BY s.sku"):
        if r["reserved"] != r["held"]:
            problems.append("reserved drift on %s: stock says %d, held rows say %d"
                            % (r["sku"], r["reserved"], r["held"]))
    return problems
