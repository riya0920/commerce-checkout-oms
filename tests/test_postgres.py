"""Real Postgres: the caveat this project repeated for four passes, retested.

Skips cleanly when no server is listening, so the suite still passes on a
machine without one. That is a real trade-off and worth naming: a skipped test
proves nothing, and the run_postgres.py report is where the numbers live.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src import contention as CT   # noqa: E402
from src import pgstore as PG      # noqa: E402


needs_pg = pytest.mark.skipif(not PG.available(),
                              reason="no Postgres on %s" % PG.DSN)


@needs_pg
def test_the_server_is_a_real_postgres():
    assert PG.server_version().split(".")[0] >= "14"


# --------------------------------------------------------------------------
# correctness -- the part that DID transfer
# --------------------------------------------------------------------------
@needs_pg
@pytest.mark.parametrize("mechanism", ["optimistic", "pessimistic"])
def test_zero_oversells_under_real_row_level_concurrency(mechanism):
    """The whole point of the mechanism. On SQLite this passed because writers
    serialise; here it passes against genuinely concurrent writers, which is a
    stronger statement of the same property."""
    skus = {"SKU-A": 25}
    PG.reset(skus)
    r = PG.flash_sale(mechanism, skus, n_workers=16, attempts_each=8)
    assert r["oversell"] == 0
    assert r["n_errors"] == 0, r["errors"]
    assert r["granted"] == 25
    assert r["reservations_held"] == 25


@needs_pg
def test_the_check_constraint_is_the_backstop_and_is_never_reached():
    """If a mechanism were wrong the database would refuse the write rather than
    record an oversell. A bug should surface as an exception, not as a number
    nobody reconciles until the warehouse does."""
    skus = {"SKU-B": 10}
    PG.reset(skus)
    r = PG.flash_sale("optimistic", skus, n_workers=8, attempts_each=6)
    assert r["n_errors"] == 0
    assert r["reserved_total"] == 10


@needs_pg
def test_reserved_never_exceeds_on_hand_in_the_table_itself():
    import psycopg
    skus = {"SKU-C": 12}
    PG.reset(skus)
    PG.flash_sale("pessimistic", skus, n_workers=10, attempts_each=5)
    with psycopg.connect(PG.DSN) as con:
        bad = con.execute(
            "SELECT count(*) FROM pg_stock WHERE reserved > on_hand").fetchone()[0]
    assert bad == 0


# --------------------------------------------------------------------------
# the behaviour that did NOT transfer
# --------------------------------------------------------------------------
@needs_pg
def test_the_optimistic_path_starves_on_a_hot_row_and_sqlite_cannot_show_it():
    """The finding. SQLite serialises writers, so a compare-and-set has nothing
    to lose a race to and the version cannot move under a caller holding the
    only write lock in the database. The optimistic mechanism's characteristic
    failure is invisible there BY CONSTRUCTION -- not smaller, invisible."""
    skus = {"SKU-HOT": 100_000}          # ample: nothing is a real sellout
    PG.reset(skus)
    pg = PG.flash_sale("optimistic", skus, n_workers=16, attempts_each=30)
    sq = CT.sqlite_flash_sale("optimistic", skus, 16, 30, "out/test_hot.db")
    assert pg["sold_out"] > 0, "postgres should starve on a hot row"
    assert sq["sold_out"] == 0, "sqlite cannot produce this contention"


@needs_pg
def test_spreading_the_same_workload_over_more_rows_removes_the_starvation():
    """Contention is a property of the data model, and that is the question the
    SQLite benchmark could not ask."""
    hot = {"SKU-HOT": 100_000}
    spread = {"SKU-%02d" % i: 100_000 for i in range(32)}
    PG.reset(hot)
    a = PG.flash_sale("optimistic", hot, n_workers=16, attempts_each=30)
    PG.reset(spread)
    b = PG.flash_sale("optimistic", spread, n_workers=16, attempts_each=30)
    assert a["sold_out"] > b["sold_out"]
    assert b["sold_out"] == 0


@needs_pg
def test_the_pessimistic_path_never_starves():
    skus = {"SKU-HOT": 100_000}
    PG.reset(skus)
    r = PG.flash_sale("pessimistic", skus, n_workers=16, attempts_each=30)
    assert r["sold_out"] == 0
    assert r["retries"] == 0


@needs_pg
def test_more_retry_budget_reduces_starvation_and_never_removes_it():
    """Optimistic concurrency converts contention into wasted work. The budget
    decides how much waste you buy the tail with; it does not decide whether the
    tail exists."""
    rows = PG.retry_budget_sweep(budgets=(4, 32), n_workers=16, attempts_each=25)
    lo, hi = rows[0], rows[-1]
    assert hi["starved_share"] < lo["starved_share"]
    assert hi["retries"] > lo["retries"]


@needs_pg
def test_the_sweep_never_reports_a_starve_as_a_sellout_by_accident():
    """Stock is ample by construction in these drills, so every `sold_out` is a
    retry exhaustion. If the stock were ever binding this whole section would be
    measuring the sellout path instead -- which is what the first version of the
    drill did, reading 7/s against 103/s purely because one arm had 40 units and
    the other 640."""
    skus = {"SKU-HOT": 100_000}
    PG.reset(skus)
    r = PG.flash_sale("optimistic", skus, n_workers=8, attempts_each=10)
    assert r["reserved_total"] < 100_000


def test_the_module_degrades_without_a_server():
    """`available()` must answer False rather than raise, or every other project
    that imports this cannot be collected on a machine without Postgres."""
    old = PG.DSN
    try:
        PG.DSN = "host=127.0.0.1 port=1 user=nobody dbname=nothing"
        assert PG.available() is False
    finally:
        PG.DSN = old
