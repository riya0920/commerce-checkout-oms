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
        emit("Start one:  .vendor/pgsql/bin/pg_ctl -D .vendor/pgdata -o '-p 55432' start")
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
                    sqlite_path=os.path.join(OUT, "contention.db"))
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
    emit("  AND SQLITE IS FASTER IN ABSOLUTE TERMS -- %.0f/s against %.0f/s on the"
         % (hot_sq.throughput, hot_pg.throughput))
    emit("  hot row. That is not a defence of SQLite, it is what an in-process")
    emit("  engine with no loopback round-trip looks like. Quoting it as a")
    emit("  database comparison would repeat the original mistake in the other")
    emit("  direction.")
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
