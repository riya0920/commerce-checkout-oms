"""Allocation priced against ML-1's forecast distribution instead of a cover
ratio. The join this project named as its remaining heuristic.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src import forward_position as FP  # noqa: E402
from src import policies as POL         # noqa: E402


needs_ml1 = pytest.mark.skipif(not FP.ml1_available(),
                               reason="ML-1 artifacts not built")


@pytest.fixture(scope="module")
def pool():
    if not FP.ml1_available():
        pytest.skip("ML-1 artifacts not built")
    return FP.error_pool()


@pytest.fixture(scope="module")
def paths(pool):
    return FP.demand_paths(4.0, 14, pool, n_samples=1200, seed=1)


# --------------------------------------------------------------------------
# the join itself
# --------------------------------------------------------------------------
@needs_ml1
def test_the_pool_is_ml1s_and_is_standardised(pool):
    assert len(pool) > 500
    assert np.isfinite(pool).all()
    assert 0.5 < pool.std() < 5.0


@needs_ml1
def test_the_demand_level_stays_se1s(pool):
    """What is borrowed is the SHAPE of the uncertainty. Importing ML-1's demand
    LEVEL would make every number in this section a property of ML-1's panel
    rather than of this allocator."""
    for level in (2.0, 4.0, 25.0):
        p = FP.demand_paths(level, 14, pool, n_samples=800, seed=0)
        assert p.mean() == pytest.approx(level, rel=1e-9)


@needs_ml1
def test_paths_are_daily_and_non_negative(paths):
    assert paths.shape == (1200, 14)
    assert (paths >= 0).all()


@needs_ml1
def test_pricing_and_scoring_halves_are_disjoint(paths):
    """A model graded on the samples that priced it is grading its own
    assumptions and wins every time without meaning anything."""
    a, b = FP.split_paths(paths)
    assert len(a) + len(b) == len(paths)
    assert not np.array_equal(a, b)


# --------------------------------------------------------------------------
# the forward position
# --------------------------------------------------------------------------
@needs_ml1
def test_more_stock_never_means_more_expected_shortfall(paths):
    prev = float("inf")
    for oh in (0, 10, 30, 60, 120, 400):
        e = FP.expected_shortfall(paths, oh)
        assert e <= prev + 1e-9
        prev = e


@needs_ml1
def test_plenty_of_stock_means_no_shortfall_and_no_penalty(paths):
    assert FP.expected_shortfall(paths, 5000) == pytest.approx(0.0, abs=1e-9)
    assert FP.scarcity_penalty_forecast_cents(paths, 5000, 6, 900) == 0


@needs_ml1
def test_inbound_stock_arriving_in_time_removes_the_shortfall(paths):
    """A DC that is low today with a truck arriving Thursday is not scarce. The
    daily walk is the whole reason this is computable -- a total-versus-total
    comparison cannot represent an arrival date."""
    late = FP.expected_shortfall(paths, 20, inbound_qty=200, inbound_day=13)
    early = FP.expected_shortfall(paths, 20, inbound_qty=200, inbound_day=1)
    assert early < late


@needs_ml1
def test_the_penalty_saturates_at_the_margin_of_the_units_shipped(paths):
    """BOTH policies have a ceiling; the difference is where it comes from. This
    one is the true economic bound -- you cannot lose more margin than the units
    you shipped were worth."""
    qty, cost = 6, 900
    assert FP.scarcity_penalty_forecast_cents(paths, 1, qty, cost) <= qty * cost
    assert FP.scarcity_penalty_forecast_cents(paths, 8, qty, cost) == qty * cost


@needs_ml1
def test_an_empty_dc_is_charged_nothing(paths):
    """Shipping from it protects nothing that was not already lost. The
    heuristic still charges its maximum, which is a proxy come loose from the
    thing it proxies for."""
    assert FP.scarcity_penalty_forecast_cents(paths, 0, 6, 900) == 0
    empty = POL.DCPosition(name="X", on_hand={"SKU": 0}, km=1.0,
                           daily_demand={"SKU": 4.0})
    assert POL.scarcity_penalty_cents(empty, "SKU", 6) == 840


@needs_ml1
def test_the_heuristic_cannot_see_variance_and_this_can(pool):
    """Days of cover is a function of the MEAN, so two DCs with identical cover
    and different variability are the same number to the heuristic."""
    steady = FP.demand_paths(4.0, 14, pool * 0.35, n_samples=1000, seed=7)
    spiky = FP.demand_paths(4.0, 14, pool * 1.9, n_samples=1000, seed=7)
    dc = POL.DCPosition(name="X", on_hand={"SKU": 70}, km=1.0,
                        daily_demand={"SKU": 4.0})
    assert POL.scarcity_penalty_cents(dc, "SKU", 6) == 0      # same for both
    lo = FP.scarcity_penalty_forecast_cents(steady, 70, 6, 900)
    hi = FP.scarcity_penalty_forecast_cents(spiky, 70, 6, 900)
    assert hi > lo * 2


@needs_ml1
def test_the_heuristic_penalty_is_continuous_at_its_floor():
    """The obvious criticism is the wrong one -- there is no cliff, there is a
    kink. Worth pinning so nobody 'fixes' a discontinuity that is not there."""
    def pen(oh):
        dc = POL.DCPosition(name="X", on_hand={"SKU": oh}, km=1.0,
                            daily_demand={"SKU": 4.0})
        return POL.scarcity_penalty_cents(dc, "SKU", 6)

    # cover after shipping 6 at 4/day: oh=34 -> exactly 7.0 days, the floor
    assert pen(35) == 0 and pen(34) == 0
    assert pen(33) <= 60             # just below the floor, still nearly zero
    assert pen(30) <= 150
    # and it climbs from there rather than jumping
    assert pen(30) < pen(20) < pen(10) <= pen(0)


@needs_ml1
def test_unserved_demand_is_lost_not_backordered(paths):
    """Consistent with the rest of this project, where an out-of-stock checkout
    returns 409 rather than queueing. If it were backordered, the shortfall on a
    path could be recovered on a later day and the walk would undercount."""
    one = np.array([[10.0, 10.0, 0.0, 0.0]])
    assert FP.expected_shortfall(one, 5.0) == pytest.approx(15.0)


@needs_ml1
def test_stockout_probability_and_shortfall_agree_about_the_easy_cases(paths):
    assert FP.stockout_probability(paths, 5000) == 0.0
    assert FP.stockout_probability(paths, 0) == 1.0
