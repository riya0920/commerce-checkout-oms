"""The same two reservation mechanisms, on real Postgres.

WHAT THIS PROJECT SAID ABOUT ITSELF, IN EVERY PASS
--------------------------------------------------
"SQLite, not Postgres, and no Postgres binary is installable in this environment.
This weakens exactly one claim -- the optimistic-vs-pessimistic contention
benchmark measures retry cost, not row-level concurrency, because both queue
behind one global write lock. The conditional-`UPDATE` shape transfers; the
throughput number does not."

The second sentence of that was wrong, and it was wrong for four passes. A
Postgres binary IS installable here: the official Windows x64 binaries are a
297 MB zip that unpacks and runs `initdb` into a local directory with no
installer, no service registration and no administrator rights. Nobody checked.
The caveat was inherited from the first pass and repeated three times without
being retested, which is exactly the failure this portfolio keeps finding in
other people's numbers.

WHAT POSTGRES MAKES MEASURABLE THAT SQLITE CANNOT
-------------------------------------------------
SQLite serialises every writer against one database-wide lock. That is not a
degenerate case of row-level locking, it is a different thing: two transactions
touching two DIFFERENT rows still queue. So on SQLite the same benchmark cannot
distinguish

    "these transactions conflict"          (a data-model property)
    "this database serialises writers"     (an engine property)

and every throughput number it produces is the second one wearing the first one's
clothes. The `contention` sweep below is the measurement that separates them:
identical workload, identical mechanisms, one row versus many.

WHAT STILL DOES NOT TRANSFER
----------------------------
Client and server are the same machine over loopback. There is no network, so
these are still a floor rather than a service latency -- the same caveat the load
generator carries, and it is unaffected by the engine.
"""
from __future__ import annotations

import os
import threading
import time
import uuid

import psycopg

DSN = os.environ.get(
    "SE1_PG_DSN", "host=127.0.0.1 port=55432 user=postgres dbname=postgres")

SCHEMA = """
DROP TABLE IF EXISTS pg_reservations;
DROP TABLE IF EXISTS pg_stock;
CREATE TABLE pg_stock (
    sku       text PRIMARY KEY,
    on_hand   integer NOT NULL,
    reserved  integer NOT NULL DEFAULT 0,
    version   integer NOT NULL DEFAULT 0,
    CONSTRAINT no_oversell CHECK (reserved <= on_hand)
);
CREATE TABLE pg_reservations (
    reservation_id text PRIMARY KEY,
    order_id       text NOT NULL,
    sku            text NOT NULL REFERENCES pg_stock(sku),
    qty            integer NOT NULL,
    state          text NOT NULL,
    created_at     double precision NOT NULL
);
"""


class OutOfStock(Exception):
    pass


def available() -> bool:
    try:
        with psycopg.connect(DSN, connect_timeout=3) as con:
            con.execute("SELECT 1")
        return True
    except Exception:
        return False


def server_version() -> str:
    with psycopg.connect(DSN) as con:
        return con.execute("SHOW server_version").fetchone()[0]


def reset(skus: dict[str, int]) -> None:
    """Fresh tables and seeded stock.

    The CHECK constraint is the point of last resort and it is deliberately kept:
    if a mechanism is wrong the database refuses the write rather than recording
    an oversell, so a bug shows up as an exception rather than as a number nobody
    reconciles until the warehouse does.
    """
    with psycopg.connect(DSN, autocommit=True) as con:
        con.execute(SCHEMA)
        with con.cursor() as cur:
            cur.executemany("INSERT INTO pg_stock(sku,on_hand) VALUES (%s,%s)",
                            list(skus.items()))


# --------------------------------------------------------------------------
# the two mechanisms -- the same shapes as the SQLite versions
# --------------------------------------------------------------------------
def reserve_optimistic(con, order_id: str, sku: str, qty: int,
                       max_retries: int = 8) -> tuple[str, int]:
    """Compare-and-set on (sku, version), exactly as in `inventory.py`.

    The UPDATE carries its own precondition, so there is no window in which two
    callers both believe they hold the last unit. On Postgres a losing writer is
    not blocked -- it reads a stale version, its UPDATE matches no row, and it
    retries. That is the mechanism, not an artefact of the engine.
    """
    for attempt in range(1, max_retries + 1):
        with con.transaction():
            row = con.execute(
                "SELECT on_hand, reserved, version FROM pg_stock WHERE sku=%s",
                (sku,)).fetchone()
            if row is None:
                raise OutOfStock("unknown sku %s" % sku)
            on_hand, reserved, version = row
            if on_hand - reserved < qty:
                raise OutOfStock("sold out")
            cur = con.execute(
                "UPDATE pg_stock SET reserved = reserved + %s, version = version + 1 "
                "WHERE sku = %s AND version = %s AND on_hand - reserved >= %s",
                (qty, sku, version, qty))
            if cur.rowcount == 1:
                rid = uuid.uuid4().hex
                con.execute(
                    "INSERT INTO pg_reservations VALUES (%s,%s,%s,%s,'held',%s)",
                    (rid, order_id, sku, qty, time.time()))
                return rid, attempt
    raise OutOfStock("contention: exhausted %d retries" % max_retries)


def reserve_pessimistic(con, order_id: str, sku: str, qty: int,
                        max_retries: int = 1) -> tuple[str, int]:
    """SELECT ... FOR UPDATE, which on Postgres is a real row lock.

    This is the arm SQLite could not represent at all. There, `BEGIN IMMEDIATE`
    takes the whole database; here the lock is on one row of one table, and two
    checkouts for different SKUs do not meet.
    """
    with con.transaction():
        row = con.execute(
            "SELECT on_hand, reserved FROM pg_stock WHERE sku=%s FOR UPDATE",
            (sku,)).fetchone()
        if row is None:
            raise OutOfStock("unknown sku %s" % sku)
        on_hand, reserved = row
        if on_hand - reserved < qty:
            raise OutOfStock("sold out")
        con.execute("UPDATE pg_stock SET reserved = reserved + %s, "
                    "version = version + 1 WHERE sku = %s", (qty, sku))
        rid = uuid.uuid4().hex
        con.execute("INSERT INTO pg_reservations VALUES (%s,%s,%s,%s,'held',%s)",
                    (rid, order_id, sku, qty, time.time()))
        return rid, 1


# --------------------------------------------------------------------------
# the drill
# --------------------------------------------------------------------------
def flash_sale(mechanism: str, skus: dict[str, int], n_workers: int,
               attempts_each: int, max_retries: int = 8) -> dict:
    """N threads race for stock. Each thread owns a connection.

    Threads rather than processes because the work is all database I/O and
    psycopg releases the GIL for it -- which is also why this is a fair
    comparison against the SQLite arm, where the GIL and the global write lock
    are precisely what is being measured.

    Timing starts when the BARRIER TRIPS, not when the threads are launched.
    Connecting to Postgres costs ~10 ms and opening a SQLite file costs almost
    nothing, so a timer started before the connects charges one engine for setup
    the other does not do -- and the first version of this did exactly that,
    reporting 71 redemptions/s for a single worker against 827/s for the same
    code in a plain loop.
    """
    reserve = (reserve_optimistic if mechanism == "optimistic"
               else reserve_pessimistic)
    sku_list = list(skus)
    granted = [0]
    sold_out = [0]
    retries = [0]
    errors: list[str] = []
    lock = threading.Lock()
    started = []
    barrier = threading.Barrier(
        n_workers, action=lambda: started.append(time.perf_counter()))

    def worker(w: int):
        try:
            with psycopg.connect(DSN) as con:
                barrier.wait()
                for i in range(attempts_each):
                    sku = sku_list[(w + i) % len(sku_list)]
                    try:
                        _, att = reserve(con, "o-%d-%d" % (w, i), sku, 1,
                                         max_retries=max_retries)
                        with lock:
                            granted[0] += 1
                            retries[0] += att - 1
                    except OutOfStock:
                        with lock:
                            sold_out[0] += 1
                    except Exception as e:      # a real failure, not a sellout
                        with lock:
                            errors.append("%s: %s" % (type(e).__name__, e))
        except Exception as e:
            with lock:
                errors.append("connect: %s" % e)

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(n_workers)]
    launched = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    done = time.perf_counter()
    # measured from the barrier, so connection setup is not charged to either
    # engine; `setup_seconds` is reported rather than hidden.
    t0 = started[0] if started else launched
    elapsed = done - t0
    setup = t0 - launched

    with psycopg.connect(DSN) as con:
        rows = con.execute(
            "SELECT sku, on_hand, reserved FROM pg_stock ORDER BY sku").fetchall()
        held = con.execute(
            "SELECT count(*) FROM pg_reservations WHERE state='held'").fetchone()[0]
    oversell = sum(max(0, r - o) for _, o, r in rows)
    return dict(mechanism=mechanism, workers=n_workers, skus=len(skus),
                max_retries=max_retries,
                granted=granted[0], sold_out=sold_out[0], retries=retries[0],
                reserved_total=sum(r for _, _, r in rows),
                reservations_held=held, oversell=oversell,
                seconds=elapsed, setup_seconds=setup, throughput=granted[0] / max(elapsed, 1e-9),
                errors=errors[:5], n_errors=len(errors))


def retry_budget_sweep(budgets=(4, 8, 16, 32, 64), n_workers: int = 16,
                       attempts_each: int = 40, stock: int = 100_000) -> list[dict]:
    """How much retry budget the optimistic mechanism needs on ONE hot row.

    This is the measurement SQLite cannot produce. There, a compare-and-set has
    nothing to lose a race to -- the engine serialises writers, so the version
    never moves under a caller and the retry budget is never tested. SE-1's
    production default of 8 was chosen against that.

    > The first version of this swept by rebinding the module-level
    > `reserve_optimistic` to a wrapper that called `reserve_optimistic`, which
    > is infinite recursion. Every worker raised RecursionError, the broad
    > `except Exception` in the drill swallowed them into the error list, and the
    > sweep reported granted 0 and starved 0.000 for every budget -- a table of
    > zeros that looks like "no starvation at any budget" rather than like a
    > crash. The parameter is threaded through instead.
    """
    skus = {"SKU-HOT": stock}
    out = []
    for mr in budgets:
        reset(skus)
        r = flash_sale("optimistic", skus, n_workers, attempts_each,
                       max_retries=mr)
        total = r["granted"] + r["sold_out"]
        if r["n_errors"]:
            raise RuntimeError("drill errored: %s" % r["errors"][0])
        out.append(dict(max_retries=mr, granted=r["granted"], attempted=total,
                        starved=r["sold_out"],
                        starved_share=r["sold_out"] / max(total, 1),
                        retries=r["retries"], seconds=r["seconds"]))
    return out
