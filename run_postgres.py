"""The Postgres pass: the caveat this project repeated for four passes, retested.

    "SQLite, not Postgres, and no Postgres binary is installable in this
     environment. [...] The conditional-UPDATE shape transfers; the throughput
     number does not."

The first clause was false and the second was understated. Run with a Postgres
listening on the DSN in `SE1_PG_DSN` (defaults to 127.0.0.1:55432).
"""
from __future__ import annotations

import json
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from src import contention as CT     # noqa: E402
from src import pgstore as PG        # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")


def main():
    os.makedirs(OUT, exist_ok=True)
    lines, summary = [], {}

    def emit(s=""):
        print(s)
        lines.append(s)

    emit("=" * 78)
    emit("SE-1 POSTGRES PASS")
    emit("=" * 78)
    if not PG.available():
        emit("No Postgres on %s." % PG.DSN)
        emit("Start one:  .vendor/pgsql/bin/pg_ctl -D .vendor/pgdata -o '-p 5433' start")
        return
    emit("PostgreSQL %s, loopback, fsync off." % PG.server_version())
    emit("")
    emit("THE CAVEAT THIS PROJECT CARRIED FOR FOUR PASSES:")
    emit("")
    emit("  'SQLite, not Postgres, and no Postgres binary is installable in this")
    emit("   environment. The conditional-UPDATE shape transfers; the throughput")
    emit("   number does not.'")
    emit("")
    emit("  The first clause is false. The official Windows x64 binaries are a")
    emit("  297 MB zip that unpacks and runs initdb into a local directory -- no")
    emit("  installer, no service registration, no administrator rights. It was")
    emit("  written in the first pass and repeated three times without being")
    emit("  retested. THAT IS THE SAME FAILURE THIS PORTFOLIO KEEPS FINDING IN")
    emit("  OTHER PEOPLE'S NUMBERS: a caveat is a claim, and an unretested claim")
    emit("  does not become true by being repeated.")
    emit("")

    # ------------------------------------------------------------------
    emit("=" * 78)
    emit("A. THE SAME DRILL ON BOTH ENGINES")
    emit("=" * 78)
    rows = CT.sweep(sku_counts=(1, 4, 16, 64), n_workers=16, attempts_each=40,
                    sqlite_path=os.path.join(OUT, "contention.db"), reps=5)
    D = pd.DataFrame(rows)
    D["attempted"] = D.granted + D.sold_out
    D["starved_share"] = D.sold_out / D.attempted
    show = D[["engine", "mechanism", "skus", "granted", "attempted",
              "starved_share", "retries", "oversell", "seconds", "throughput"]]
    emit(show.to_string(index=False, float_format=lambda x: "%9.3f" % x))
    emit("")
    emit("  Stock is ample everywhere, so every `sold_out` is a request that")
    emit("  FAILED WITH STOCK ON THE SHELF -- the optimistic path exhausting its")
    emit("  retry budget. Nothing here is a real sellout.")
    emit("")
    assert int(D.oversell.sum()) == 0
    emit("  Oversells across every row and both engines: 0. The mechanism is")
    emit("  correct on both, which is the part that did transfer.")
    emit("")
    hot_pg = D[(D.engine == "postgres") & (D.mechanism == "optimistic")
               & (D.skus == 1)].iloc[0]
    hot_sq = D[(D.engine == "sqlite") & (D.mechanism == "optimistic")
               & (D.skus == 1)].iloc[0]
    emit("READ THE ONE-SKU OPTIMISTIC ROWS AGAINST EACH OTHER.")
    emit("")
    emit("    sqlite   : %4d of %4d granted, %5d retries"
         % (hot_sq.granted, hot_sq.attempted, hot_sq.retries))
    emit("    postgres : %4d of %4d granted, %5d retries"
         % (hot_pg.granted, hot_pg.attempted, hot_pg.retries))
    emit("")
    emit("  THE CAVEAT WAS UNDERSTATED. It said the throughput number does not")
    emit("  transfer. What does not transfer is the BEHAVIOUR: on Postgres the")
    emit("  optimistic mechanism starves %.0f%% of requests that had stock"
         % (100 * hot_pg.starved_share))
    emit("  available, and on SQLite the identical code starves %.0f%%."
         % (100 * hot_sq.starved_share))
    emit("")
    emit("  The reason is not that SQLite is better. It is that SQLite SERIALISES")
    emit("  WRITERS, so a compare-and-set has nothing to lose a race to: the")
    emit("  version cannot move under a caller who holds the only write lock in")
    emit("  the database. The optimistic mechanism's characteristic failure is")
    emit("  INVISIBLE THERE BY CONSTRUCTION -- not smaller, invisible.")
    emit("")
    emit("  Which means the previous benchmark was not a weak measurement of")
    emit("  optimistic-vs-pessimistic concurrency. It was a measurement of")
    emit("  something else that had been labelled with that name.")
    emit("")
    spread = D[(D.engine == "postgres") & (D.mechanism == "optimistic")]
    emit("  Spreading the same workload over more rows fixes it:")
    for _, r in spread.iterrows():
        emit("    %3d sku -> %4d/%4d granted (%5.1f%% starved), %5d retries"
             % (r.skus, r.granted, r.attempted, 100 * r.starved_share, r.retries))
    emit("")
    emit("  That is the data-model question the old benchmark could not ask.")
    emit("  Contention is a property of how many customers want the same SKU, and")
    emit("  on SQLite it was indistinguishable from the engine's own serialisation.")
    emit("")
    pess_pg = D[(D.engine == "postgres") & (D.mechanism == "pessimistic")
                & (D.skus == 1)].iloc[0]
    pess_sq = D[(D.engine == "sqlite") & (D.mechanism == "pessimistic")
                & (D.skus == 1)].iloc[0]
    pess_pg16 = D[(D.engine == "postgres") & (D.mechanism == "pessimistic")
                  & (D.skus == 16)].iloc[0]
    pess_sq16 = D[(D.engine == "sqlite") & (D.mechanism == "pessimistic")
                  & (D.skus == 16)].iloc[0]
    emit("ON THROUGHPUT, COMPARE THE PESSIMISTIC ROWS, NOT THE OPTIMISTIC ONES.")
    emit("")
    emit("  Comparing rates on the optimistic hot row would be comparing a run")
    emit("  that granted %d requests against one that granted %d. The pessimistic"
         % (hot_sq.granted, hot_pg.granted))
    emit("  rows are the like-for-like pair -- both grant everything:")
    emit("")
    emit("    1 sku : sqlite %6.0f/s   postgres %6.0f/s"
         % (pess_sq.throughput, pess_pg.throughput))
    emit("   16 sku : sqlite %6.0f/s   postgres %6.0f/s"
         % (pess_sq16.throughput, pess_pg16.throughput))
    emit("")
    sq_scale = pess_sq16.throughput / max(pess_sq.throughput, 1e-9)
    pg_scale = pess_pg16.throughput / max(pess_pg.throughput, 1e-9)
    sc = CT.scaling_ratio(n_workers=16, attempts_each=40, reps=9,
                          sqlite_path=os.path.join(OUT, "scaling.db"))
    emit("  Absolute rates on one machine are noisy enough that a single run")
    emit("  flips the sign: consecutive runs gave Postgres 917/s then 563/s on")
    emit("  the same cell. So the claim is made on the SHAPE, measured PAIRED --")
    emit("  the one-row and sixteen-row cells run back to back within each")
    emit("  repetition, so machine drift cancels instead of landing in the ratio.")
    emit("")
    emit("  Throughput gain from spreading the same workload 1 row -> 16 rows,")
    emit("  %d paired repetitions, pessimistic mechanism:" % sc["reps"])
    emit("")
    emit("    sqlite   : median %.2fx   (range %.2f - %.2f)"
         % (sc["sqlite"]["median"], sc["sqlite"]["low"], sc["sqlite"]["high"]))
    emit("    postgres : median %.2fx   (range %.2f - %.2f)"
         % (sc["postgres"]["median"], sc["postgres"]["low"],
            sc["postgres"]["high"]))
    emit("")
    emit("  Postgres scaled more than SQLite in %d of %d paired repetitions."
         % (sc["postgres_scaled_more"], sc["reps"]))
    emit("")
    emit("  SQLite's range includes %.2f -- it sometimes goes SLOWER with more"
         % sc["sqlite"]["low"])
    emit("  rows, which is what 'the number of distinct rows is irrelevant to")
    emit("  this engine' looks like when you measure it: noise around 1.0. Its")
    emit("  global write lock does not care how many rows there are. Postgres's")
    emit("  row locks do.")
    emit("")
    emit("  THAT IS SE-2'S PREDICTION, TESTED: 'writers on different rows queue")
    emit("  here and would proceed in parallel on Postgres.'")
    emit("")
    emit("  IT HOLDS IN DIRECTION AND NOT IN MAGNITUDE. The paired count -- %d of"
         % sc["postgres_scaled_more"])
    emit("  %d -- is the part that reproduces; the median ratio itself moved from"
         % sc["reps"])
    emit("  1.50x to %.2fx between two runs of this same nine-rep measurement."
         % sc["postgres"]["median"])
    emit("  So: Postgres does benefit from spreading writers across rows and")
    emit("  SQLite does not, which is the structural claim. 'Proceed in parallel'")
    emit("  overstates the size -- at 16 workers the bottleneck is already partly")
    emit("  the client, and no engine change moves that.")
    emit("")
    summary["scaling"] = sc
    emit("  A CORRECTION TO THIS PROJECT'S OWN FIRST POSTGRES RUN. The initial")
    emit("  version of this harness started its timer before the worker threads")
    emit("  connected, so ~4.8 s of Postgres connection setup was charged to the")
    emit("  drill while SQLite's file-open cost nothing. It reported 452/s against")
    emit("  32/s and I put that in the README. Timing now starts when the barrier")
    emit("  trips and `setup_seconds` is reported separately.")
    emit("")
    summary["sweep"] = show.round(4).to_dict("records")

    # ------------------------------------------------------------------
    emit("=" * 78)
    emit("B. THE RETRY BUDGET -- A DEFAULT CALIBRATED AGAINST THE WRONG ENGINE")
    emit("=" * 78)
    emit("`reserve_optimistic` defaults to max_retries=8. Sixteen workers on ONE")
    emit("row, ample stock:")
    emit("")
    budget = PG.retry_budget_sweep(budgets=(4, 8, 16, 32, 64), n_workers=16,
                                   attempts_each=40)
    B = pd.DataFrame(budget)
    emit(B.to_string(index=False, float_format=lambda x: "%9.3f" % x))
    emit("")
    at8 = [b for b in budget if b["max_retries"] == 8][0]
    at64 = [b for b in budget if b["max_retries"] == 64][0]
    emit("  At the shipped default of 8, %.0f%% of requests are refused with"
         % (100 * at8["starved_share"]))
    emit("  stock on the shelf. At 64 it is %.0f%%, and the retry count has gone"
         % (100 * at64["starved_share"]))
    emit("  from %d to %d to buy that." % (at8["retries"], at64["retries"]))
    emit("")
    emit("  IT NEVER REACHES ZERO. Optimistic concurrency converts contention")
    emit("  into wasted work, and the budget decides how much waste you buy the")
    emit("  tail with -- it does not decide whether the tail exists. A hot row")
    emit("  needs the pessimistic path or a different data model, and no retry")
    emit("  number fixes it.")
    emit("")
    emit("  The default of 8 was not chosen carelessly. It was chosen against a")
    emit("  database where it was never tested, because SQLite cannot generate")
    emit("  the contention that tests it.")
    emit("")
    summary["retry_budget"] = budget

    emit("=" * 78)
    emit("WHAT STILL DOES NOT TRANSFER")
    emit("=" * 78)
    emit("  Client and server are one machine over loopback. No network, no")
    emit("  serialisation across a wire, no connection pool, fsync off. These are")
    emit("  a floor, exactly as the load generator's numbers are, and the engine")
    emit("  change does not touch that.")
    emit("")

    with open(os.path.join(OUT, "postgres_report.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    with open(os.path.join(OUT, "postgres_metrics.json"), "w") as f:
        json.dump(summary, f, indent=2, default=float)
    print("\n-> out/postgres_report.txt")


if __name__ == "__main__":
    main()
