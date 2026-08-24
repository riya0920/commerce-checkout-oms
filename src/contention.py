"""The same contention drill on both engines, so the engine is the only variable.

THE QUESTION THIS ANSWERS AND THE OLD BENCHMARK COULD NOT
---------------------------------------------------------
Does the optimistic mechanism's cost come from the DATA (two checkouts wanting
the same SKU) or from the ENGINE (a database that serialises all writers)?

On SQLite those are the same event, so the question is unaskable: `BEGIN
IMMEDIATE` takes the whole database, two checkouts for different SKUs queue
anyway, and a version never changes under a writer because no other writer is
running. **The optimistic mechanism's weakness cannot appear on SQLite at all**
-- not "appears smaller", cannot appear -- because the compare-and-set has
nothing to lose a race to.

The sweep varies one thing: how many distinct rows the same number of workers
spread across. Identical mechanisms, identical worker count, identical total
attempts.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time

from src import db as DB
from src import inventory as INV


def sqlite_flash_sale(mechanism: str, skus: dict[str, int], n_workers: int,
                      attempts_each: int, path: str) -> dict:
    """SE-1's own reservation code, driven by threads with per-thread connections.

    Uses `inventory.reserve_optimistic` / `reserve_pessimistic` unchanged: the
    point is to measure this project's actual code on a different engine, not a
    reimplementation that might differ in some way nobody noticed.
    """
    if os.path.exists(path):
        os.remove(path)
    con = DB.init(path, fresh=True)
    for sku, qty in skus.items():
        DB.seed_stock(con, sku, qty)
    con.commit()
    con.close()

    reserve = (INV.reserve_optimistic if mechanism == "optimistic"
               else INV.reserve_pessimistic)
    sku_list = list(skus)
    granted, sold_out, retries = [0], [0], [0]
    errors: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(n_workers)

    def worker(w: int):
        c = DB.connect(path)
        try:
            barrier.wait()
            for i in range(attempts_each):
                sku = sku_list[(w + i) % len(sku_list)]
                try:
                    _, att = reserve(c, "o-%d-%d" % (w, i), sku, 1)
                    c.commit()
                    with lock:
                        granted[0] += 1
                        retries[0] += att - 1
                except INV.OutOfStock:
                    try:
                        c.rollback()
                    except Exception:
                        pass
                    with lock:
                        sold_out[0] += 1
                except Exception as e:
                    try:
                        c.rollback()
                    except Exception:
                        pass
                    with lock:
                        errors.append("%s: %s" % (type(e).__name__, e))
        finally:
            c.close()

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(n_workers)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - t0

    c = DB.connect(path)
    rows = c.execute("SELECT sku, on_hand, reserved FROM stock ORDER BY sku").fetchall()
    c.close()
    oversell = sum(max(0, r["reserved"] - r["on_hand"]) for r in rows)
    return dict(engine="sqlite", mechanism=mechanism, workers=n_workers,
                skus=len(skus), granted=granted[0], sold_out=sold_out[0],
                retries=retries[0], oversell=oversell, seconds=elapsed,
                throughput=granted[0] / max(elapsed, 1e-9),
                errors=errors[:5], n_errors=len(errors))


def sweep(sku_counts=(1, 4, 16, 64), n_workers: int = 32,
          attempts_each: int = 60, stock: int = 100_000,
          sqlite_path: str = "out/contention.db") -> list[dict]:
    """Both engines, both mechanisms, across contention levels.

    Stock is deliberately ample. A drill that sells out measures the sold-out
    path, and `granted / elapsed` then reports how much stock there was rather
    than how fast the engine went -- the first version of this did exactly that
    and read 7/s against 103/s purely because one arm had 40 units and the other
    had 640.
    """
    from src import pgstore as PG
    out = []
    have_pg = PG.available()
    for n in sku_counts:
        skus = {"SKU-%03d" % i: stock for i in range(n)}
        for mech in ("optimistic", "pessimistic"):
            r = sqlite_flash_sale(mech, skus, n_workers, attempts_each,
                                  sqlite_path)
            out.append(r)
            if have_pg:
                PG.reset(skus)
                p = PG.flash_sale(mech, skus, n_workers, attempts_each)
                p["engine"] = "postgres"
                out.append(p)
    return out
