"""What the load generator actually calls. Kept in its own module because the
client runs in a SPAWNED process and every symbol it touches must be importable
by name -- a closure over a live database handle cannot cross a process boundary,
and discovering that at the pickling error is a worse way to learn it.
"""
from __future__ import annotations

import os
import sqlite3
import time

from . import checkout as CO
from . import db as DB
from . import psp as PSP


def setup(db_path: str, mechanism: str = "optimistic"):
    """One CONNECTION PER PROCESS, which is the point of the separate process.

    A shared connection across threads was the thing that made the previous
    benchmark measure Python's scheduler: SQLite serialises on a connection, so
    every client thread queued behind every other regardless of what the database
    was doing.
    """
    con = DB.connect(db_path)
    return dict(con=con, psp=PSP.FakePSP(seed=os.getpid()), mechanism=mechanism)


def teardown(ctx):
    try:
        ctx["con"].close()
    except Exception:
        pass


def flash_sale_checkout(ctx, worker_id: int, i: int):
    """One shopper attempting to buy one unit of the contended SKU."""
    order_id = "w%d-%d" % (worker_id, i)
    try:
        res = CO.checkout(
            ctx["con"], ctx["psp"], customer_id="c%d-%d" % (worker_id, i),
            lines=[dict(sku="FLASH", qty=1, unit_price=1000)],
            idempotency_key=order_id, mechanism=ctx["mechanism"])
        return True, res.get("state", "placed")
    except CO.CheckoutError as exc:
        # An out-of-stock is a SUCCESSFUL request with a business outcome, not a
        # failure. Counting it as an error is how a flash sale reports a 90%
        # error rate and a working system.
        return True, str(exc)[:40]
    except sqlite3.OperationalError as exc:
        return False, "db_" + str(exc)[:30]


def read_only_lookup(ctx, worker_id: int, i: int):
    """A read, for contrast: the same harness against work that does not write."""
    cur = ctx["con"].execute("SELECT on_hand, reserved FROM stock WHERE sku = ?",
                             ("FLASH",))
    row = cur.fetchone()
    return row is not None, "read"
