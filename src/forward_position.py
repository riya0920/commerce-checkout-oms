"""Allocation priced against a FORECAST DISTRIBUTION rather than a cover ratio.

WHAT THIS PROJECT SAID ABOUT ITSELF
-----------------------------------
"Allocation is still a heuristic. The scarcity penalty is a cover-shortfall
charge, not a forward-position model -- the real version needs a demand forecast
per DC, which is ML-1's job and is not wired in."

This wires it in. It is the second consumer of ML-1's block bootstrap; DATA-2 is
the first, and the two use it for different things. DATA-2 needs demand SUMMED
over a lead time, to size an order-up-to level. This needs the DAILY path,
because the question is not "how much will be needed" but "will this DC run out
before the truck arrives" -- and a replenishment landing on day 4 decides which
shortfalls happen. ML-1 exposes `leadtime_daily_paths` for exactly that, and
`leadtime_samples` delegates to it so there is one bootstrap and not two.

WHAT IS BORROWED AND WHAT IS NOT
--------------------------------
The DEMAND LEVEL stays SE-1's: this project's SKUs are not M5 items and pretending
otherwise would be a fake join. What is borrowed is the SHAPE of the uncertainty
-- the standardised forecast-error distribution, its dispersion, and the
autocorrelation the block bootstrap preserves. Each sampled path is rescaled so
its mean matches the DC's own daily demand for that SKU.

That is the honest form of this join. Saying "safety stock now uses a real
forecast" while silently importing somebody else's demand level would make every
number here a property of ML-1's panel rather than of this allocator.

THE TRAP THIS MODULE IS BUILT TO AVOID
--------------------------------------
A forecast-based penalty scored on the same sample paths that priced it will
always win, and the win means nothing: it is being graded on its own assumptions.
`split_paths` divides the bootstrap into a PRICING half and a SCORING half, and
the report uses them in that order. The heuristic and myopic allocators are
scored on the same held-out half, so all three face identical futures.
"""
from __future__ import annotations

import importlib.util
import os
import pickle

import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ML1 = os.path.join(HERE, os.pardir, "ml1-forecast-to-price")

_CACHE = {}


def ml1_available() -> bool:
    return os.path.exists(os.path.join(ML1, "out", "quantile_raw.pkl"))


def _ml1_leadtime():
    """Import ML-1's module BY FILE PATH.

    Both projects have a top-level package called `src`, so a plain sys.path
    insert resolves `src.leadtime` against whichever `src` was imported first --
    a silent wrong-module import in the general case. DATA-2 hit this from the
    other side and the note is repeated here because the failure is quiet.
    """
    if "lt" in _CACHE:
        return _CACHE["lt"]
    path = os.path.join(ML1, "src", "leadtime.py")
    if not os.path.exists(path):
        raise ImportError("ml1-forecast-to-price/src/leadtime.py not found")
    spec = importlib.util.spec_from_file_location("ml1_leadtime_se1", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _CACHE["lt"] = mod
    return mod


def error_pool(fold: int | None = None, max_series: int = 120) -> np.ndarray:
    """Standardised forecast errors from ML-1, pooled across series IN TIME ORDER.

    Order is the information the block bootstrap exists to use. Shuffling this
    array turns the method back into iid sampling without raising anything, which
    is why the pooling appends whole series rather than interleaving them.
    """
    lt = _ml1_leadtime()
    with open(os.path.join(ML1, "out", "quantile_raw.pkl"), "rb") as f:
        rec = pickle.load(f)
    if fold is None:
        fold = max(r["fold"] for r in rec)
    rows = [r for r in rec if r["fold"] == fold][:max_series]
    chunks = []
    for r in rows:
        point = np.asarray(r["mean_fc"], float)
        actual = np.asarray(r["actual"], float)
        chunks.append(lt.standardise(actual - point, point))
    pool = np.concatenate(chunks) if chunks else np.zeros(0)
    return pool[np.isfinite(pool)]


def demand_paths(daily_demand: float, horizon: int, pool: np.ndarray,
                 n_samples: int = 2000, seed: int = 0) -> np.ndarray:
    """(n_samples, horizon) daily demand for one SKU at one DC.

    The point path is flat at this DC's own daily demand, and ML-1's bootstrap
    supplies the deviation around it. Rescaled so the sample mean matches the
    level exactly -- a bootstrap pool with a small mean offset would otherwise
    read as a demand trend nobody forecast.
    """
    lt = _ml1_leadtime()
    point = np.full(int(horizon), float(daily_demand))
    paths = lt.leadtime_daily_paths(point, pool, n_samples=n_samples, seed=seed)
    m = paths.mean()
    if m > 0:
        paths = paths * (float(daily_demand) / m)
    return paths


def split_paths(paths: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Pricing half, scoring half. See the module docstring -- a model graded on
    the samples that priced it is grading its own assumptions."""
    half = len(paths) // 2
    return paths[:half], paths[half:]


# --------------------------------------------------------------------------
# the forward position
# --------------------------------------------------------------------------
def expected_shortfall(paths: np.ndarray, on_hand: float,
                       inbound_qty: float = 0.0,
                       inbound_day: int = 10 ** 6) -> float:
    """Expected units of demand that cannot be served over the horizon.

    Walks the path day by day rather than comparing a total, because the
    replenishment lands on a specific day: stock can be short on Tuesday and
    healthy on Friday, and a total-versus-total comparison books that as fine.

    Unserved demand is LOST, not backordered -- consistent with the rest of this
    project, where an out-of-stock checkout returns 409 rather than queueing.
    """
    paths = np.asarray(paths, float)
    n, horizon = paths.shape
    stock = np.full(n, float(on_hand))
    short = np.zeros(n)
    for d in range(horizon):
        if d == inbound_day:
            stock += float(inbound_qty)
        want = paths[:, d]
        served = np.minimum(stock, want)
        short += want - served
        stock -= served
    return float(short.mean())


def stockout_probability(paths: np.ndarray, on_hand: float,
                         inbound_qty: float = 0.0,
                         inbound_day: int = 10 ** 6) -> float:
    paths = np.asarray(paths, float)
    n, horizon = paths.shape
    stock = np.full(n, float(on_hand))
    hit = np.zeros(n, dtype=bool)
    for d in range(horizon):
        if d == inbound_day:
            stock += float(inbound_qty)
        want = paths[:, d]
        hit |= want > stock + 1e-9
        stock -= np.minimum(stock, want)
    return float(hit.mean())


def scarcity_penalty_forecast_cents(paths: np.ndarray, available: int, qty: int,
                                    stockout_cost_cents: int,
                                    inbound_qty: float = 0.0,
                                    inbound_day: int = 10 ** 6) -> int:
    """The MARGINAL expected cost of shipping `qty` from this DC today.

    Not a cover ratio and not a threshold: the difference in expected lost units
    between holding the stock and shipping it, priced at the merchant's own
    stockout cost. It is zero when the DC has plenty -- because the marginal unit
    changes nothing -- and it rises smoothly rather than at a 7-day cliff.

    The heuristic it replaces charges by how far below a cover floor the DC lands,
    which is a function of the MEAN only. Two DCs with the same days of cover and
    different demand variability are the same number to it and different numbers
    here, and the second reading is the one that is true.
    """
    base = expected_shortfall(paths, available, inbound_qty, inbound_day)
    after = expected_shortfall(paths, max(available - qty, 0), inbound_qty,
                               inbound_day)
    return int(round(max(after - base, 0.0) * stockout_cost_cents))


def realised_shortfall(path: np.ndarray, on_hand: float,
                       inbound_qty: float = 0.0,
                       inbound_day: int = 10 ** 6) -> float:
    """One realised future, for scoring. Same walk, a single path."""
    return expected_shortfall(np.asarray(path, float)[None, :], on_hand,
                              inbound_qty, inbound_day)
