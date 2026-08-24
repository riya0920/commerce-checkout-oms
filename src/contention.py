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

    Timing starts when the BARRIER TRIPS, not when the threads are launched.
    Connecting to Postgres costs ~10 ms and opening a SQLite file costs almost
    nothing, so a timer started before the connects charges one engine for setup
    the other does not do -- and the first version of this did exactly that,
    reporting 71 redemptions/s for a single worker against 827/s for the same
    code in a plain loop.
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
    started = []
    barrier = threading.Barrier(
        n_workers, action=lambda: started.append(time.perf_counter()))

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
          sqlite_path: str = "out/contention.db", reps: int = 3) -> list[dict]:
    """Both engines, both mechanisms, across contention levels.

    Stock is deliberately ample. A drill that sells out measures the sold-out
    path, and `granted / elapsed` then reports how much stock there was rather
    than how fast the engine went -- the first version of this did exactly that
    and read 7/s against 103/s purely because one arm had 40 units and the other
    had 640.

    REPEATED, and the median is reported. A single repetition of this drill is
    noisy enough to flip the sign of the engine comparison: consecutive runs gave
    Postgres 917/s and then 563/s on the same one-row pessimistic cell, against
    SQLite's 623/s and 618/s. Reporting one run would have made the headline a
    coin toss, which is the failure this portfolio has already caught twice
    elsewhere -- once in a convergence rate and once in a cluster-SE threshold.
    """
    import statistics

    from src import pgstore as PG
    out = []
    have_pg = PG.available()
    for n in sku_counts:
        skus = {"SKU-%03d" % i: stock for i in range(n)}
        for mech in ("optimistic", "pessimistic"):
            arms = {"sqlite": []}
            if have_pg:
                arms["postgres"] = []
            for _ in range(reps):
                arms["sqlite"].append(
                    sqlite_flash_sale(mech, skus, n_workers, attempts_each,
                                      sqlite_path))
                if have_pg:
                    PG.reset(skus)
                    p = PG.flash_sale(mech, skus, n_workers, attempts_each)
                    p["engine"] = "postgres"
                    arms["postgres"].append(p)
            for engine, runs in arms.items():
                agg = dict(runs[0])
                agg["engine"] = engine
                agg["reps"] = reps
                for key in ("granted", "sold_out", "retries", "oversell",
                            "seconds", "throughput"):
                    agg[key] = statistics.median(r[key] for r in runs)
                agg["throughput_min"] = min(r["throughput"] for r in runs)
                agg["throughput_max"] = max(r["throughput"] for r in runs)
                out.append(agg)
    return out


def scaling_ratio(n_workers: int = 16, attempts_each: int = 40,
                  stock: int = 100_000, reps: int = 9,
                  sqlite_path: str = "out/scaling.db") -> dict:
    """Does spreading the SAME workload across more rows buy anything?

    PAIRED within a repetition: the one-row and sixteen-row cells for an engine
    run back to back, so machine drift cancels instead of landing in the ratio.
    Unpaired medians of this were not stable -- successive five-rep runs put the
    Postgres ratio at 1.76x and then 1.34x, which is not a measurement, it is a
    number that happened.

    The pessimistic mechanism only, because it is the like-for-like arm: the
    optimistic one starves on the hot row, and comparing a rate that granted 640
    against one that granted 306 is comparing two different experiments.
    """
    import statistics

    from src import pgstore as PG
    one = {"SKU-000": stock}
    many = {"SKU-%03d" % i: stock for i in range(16)}
    have_pg = PG.available()
    ratios: dict[str, list[float]] = {"sqlite": []}
    if have_pg:
        ratios["postgres"] = []
    for _ in range(reps):
        a = sqlite_flash_sale("pessimistic", one, n_workers, attempts_each,
                              sqlite_path)
        b = sqlite_flash_sale("pessimistic", many, n_workers, attempts_each,
                              sqlite_path)
        ratios["sqlite"].append(b["throughput"] / max(a["throughput"], 1e-9))
        if have_pg:
            PG.reset(one)
            pa = PG.flash_sale("pessimistic", one, n_workers, attempts_each)
            PG.reset(many)
            pb = PG.flash_sale("pessimistic", many, n_workers, attempts_each)
            ratios["postgres"].append(
                pb["throughput"] / max(pa["throughput"], 1e-9))
    out = {"reps": reps}
    for engine, vals in ratios.items():
        out[engine] = dict(median=statistics.median(vals),
                           low=min(vals), high=max(vals))
    if have_pg:
        out["postgres_scaled_more"] = sum(
            1 for x, y in zip(ratios["postgres"], ratios["sqlite"]) if x > y)
    return out
