"""Tests for the completion pass: per-line tax, policy tables, forward-looking
allocation, the time-bucketed funnel, the load generator, and the HTTP surface."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src import db as DB            # noqa: E402
from src import lifecycle as LC     # noqa: E402
from src import loadgen as LG       # noqa: E402
from src import policies as POL     # noqa: E402
from src import taxpolicy as TAX    # noqa: E402


# --------------------------------------------------------------------------
# per-line tax
# --------------------------------------------------------------------------
def test_exempt_lines_are_not_taxed():
    out = TAX.cart_tax([dict(sku="MILK", category="grocery", gross=1000,
                             discount=0)])
    assert out["tax_total"] == 0


def test_a_single_cart_rate_overcharges_a_mixed_cart():
    """The claim the section is built on: there is no correct single rate when
    the lines differ."""
    out = TAX.cart_tax([
        dict(sku="TEE", category="apparel", gross=5000, discount=0),
        dict(sku="MILK", category="grocery", gross=2000, discount=0)])
    assert out["overcharge_from_single_rate"] > 0
    assert out["single_rate_would_charge"] > out["tax_total"]


def test_tax_applies_after_a_retailer_discount():
    full = TAX.cart_tax([dict(sku="T", category="apparel", gross=1000, discount=0)])
    disc = TAX.cart_tax([dict(sku="T", category="apparel", gross=1000, discount=200)])
    assert disc["tax_total"] < full["tax_total"]


def test_a_manufacturer_coupon_does_not_reduce_the_taxable_receipt():
    """The retailer is reimbursed, so the customer is taxed on the full price.
    A flag rather than prose, because it changes the number."""
    retailer = TAX.cart_tax([dict(sku="T", category="apparel", gross=1000,
                                  discount=200)])
    manuf = TAX.cart_tax([dict(sku="T", category="apparel", gross=1000,
                               discount=200, manufacturer_funded=True)])
    assert manuf["tax_total"] > retailer["tax_total"]


def test_line_tax_rounds_half_up_at_the_line():
    assert TAX.line_tax(1000, 875) == 88          # 87.5 -> 88
    assert TAX.line_tax(0, 875) == 0


def test_refundable_tax_is_pro_rata_on_the_lines_own_rate():
    """Refunding ORDER tax pro rata by value would hand back tax on groceries
    that were never taxed."""
    ln = TAX.cart_tax([dict(sku="T", category="apparel", gross=4000,
                            discount=0)])["lines"][0]
    assert TAX.refundable_tax(ln, 2, 4) == pytest.approx(ln["tax"] / 2, abs=1)
    assert TAX.refundable_tax(ln, 0, 4) == 0


# --------------------------------------------------------------------------
# repricing
# --------------------------------------------------------------------------
def test_a_price_drop_is_always_passed_on():
    d = POL.reprice_decision(5000, 4500, "apparel")
    assert d["decision"] == "reduced" and d["charge"] == 4500


def test_the_tolerance_is_the_smaller_of_cap_and_percentage():
    """The percentage binds on expensive items, the cash cap on cheap ones."""
    cheap = POL.reprice_decision(500, 505, "apparel", "standard")
    dear = POL.reprice_decision(120000, 122000, "electronics", "standard")
    assert cheap["tolerance"] == min(300, 500 * 500 // 10000)
    assert dear["decision"] == "reconfirm"


def test_tier_changes_the_outcome_on_the_same_rise():
    std = POL.reprice_decision(120000, 122000, "electronics", "standard")
    gold = POL.reprice_decision(120000, 122000, "electronics", "gold")
    assert std["decision"] == "reconfirm"
    assert gold["decision"] == "honoured"


def test_reconfirm_returns_no_charge_rather_than_a_guess():
    d = POL.reprice_decision(5000, 9000, "apparel")
    assert d["decision"] == "reconfirm" and d["charge"] is None


def test_an_unknown_category_falls_back_rather_than_crashing():
    d = POL.reprice_decision(5000, 5100, "nonexistent")
    assert d["decision"] in {"honoured", "reconfirm"}


# --------------------------------------------------------------------------
# returns
# --------------------------------------------------------------------------
def test_a_return_outside_the_window_is_refused():
    q = POL.return_quote(category="apparel", days_since_delivery=200,
                         is_full_return=True, merch_refund_cents=5000,
                         shipping_paid_cents=795)
    assert q["eligible"] is False and q["refund"] == 0


def test_shipping_comes_back_on_a_full_return_only():
    full = POL.return_quote(category="apparel", days_since_delivery=5,
                            is_full_return=True, merch_refund_cents=5000,
                            shipping_paid_cents=795)
    part = POL.return_quote(category="apparel", days_since_delivery=5,
                            is_full_return=False, merch_refund_cents=2000,
                            shipping_paid_cents=795)
    assert full["shipping_refund"] == 795
    assert part["shipping_refund"] == 0


def test_restocking_applies_only_outside_the_grace_period():
    fast = POL.return_quote(category="electronics", days_since_delivery=3,
                            is_full_return=True, merch_refund_cents=10000,
                            shipping_paid_cents=0)
    slow = POL.return_quote(category="electronics", days_since_delivery=20,
                            is_full_return=True, merch_refund_cents=10000,
                            shipping_paid_cents=0)
    assert fast["restocking_fee"] == 0
    assert slow["restocking_fee"] > 0


def test_grocery_never_refunds_shipping_even_on_a_full_return():
    q = POL.return_quote(category="grocery", days_since_delivery=1,
                         is_full_return=True, merch_refund_cents=1000,
                         shipping_paid_cents=500)
    assert q["shipping_refund"] == 0


# --------------------------------------------------------------------------
# exchanges
# --------------------------------------------------------------------------
def test_an_even_exchange_moves_no_money_at_all():
    s = POL.exchange_settlement(3263, 3263)
    assert s["movement"] == "none" and s["net"] == 0


def test_an_upgrade_captures_only_the_difference():
    s = POL.exchange_settlement(3263, 3915)
    assert s["movement"] == "capture" and s["amount"] == 652


def test_a_small_downgrade_becomes_store_credit_not_a_refund():
    """Below a floor the refund costs more to process than it returns."""
    s = POL.exchange_settlement(3263, 3200, min_refund_cents=100)
    assert s["movement"] == "store_credit" and s["credit"] == 63


def test_a_large_downgrade_is_refunded():
    s = POL.exchange_settlement(3915, 3263, min_refund_cents=100)
    assert s["movement"] == "refund" and s["amount"] == 652


def test_the_exchange_window_is_enforced():
    assert POL.exchange_eligible(10, "apparel")["eligible"]
    assert not POL.exchange_eligible(200, "apparel")["eligible"]


# --------------------------------------------------------------------------
# forward-looking allocation
# --------------------------------------------------------------------------
def test_a_scarce_dc_is_penalised_and_a_deep_one_is_not():
    scarce = POL.DCPosition("A", on_hand={"S": 10}, daily_demand={"S": 4.0})
    deep = POL.DCPosition("B", on_hand={"S": 400}, daily_demand={"S": 4.0})
    assert POL.scarcity_penalty_cents(scarce, "S", 3) > 0
    assert POL.scarcity_penalty_cents(deep, "S", 3) == 0


def test_the_penalty_grows_with_the_quantity_taken():
    dc = POL.DCPosition("A", on_hand={"S": 20}, daily_demand={"S": 4.0})
    assert (POL.scarcity_penalty_cents(dc, "S", 10) >
            POL.scarcity_penalty_cents(dc, "S", 2))


def test_a_dc_with_no_demand_history_is_never_penalised():
    """Dividing by zero demand would make every unknown SKU look infinitely
    scarce, which is the wrong default: no data is not the same as no stock."""
    dc = POL.DCPosition("A", on_hand={"S": 1}, daily_demand={})
    assert POL.scarcity_penalty_cents(dc, "S", 1) == 0


def test_committed_stock_is_not_available():
    dc = POL.DCPosition("A", on_hand={"S": 10}, committed={"S": 8},
                        daily_demand={"S": 1.0})
    assert dc.available("S") == 2


def test_inbound_stock_counts_only_if_it_arrives_soon():
    dc = POL.DCPosition("A", on_hand={"S": 1}, inbound={"S": (100, 3)})
    assert POL.inbound_relief(dc, "S", within_days=7) == 100
    assert POL.inbound_relief(dc, "S", within_days=1) == 0


# --------------------------------------------------------------------------
# the time-bucketed funnel
# --------------------------------------------------------------------------
def _seeded_db(tmp_path):
    path = str(tmp_path / "funnel.db")
    con = DB.init(path, fresh=True)
    con.execute("INSERT INTO orders (order_id, state, customer_id, created_at,"
                " updated_at) VALUES ('o1','placed','c',0,0)")
    now = 1000.0
    rows = []
    for b in range(6):
        for i in range(10):
            oid = "o-%d-%d" % (b, i)
            rows.append((oid, None, "pending", "", now + b * 60 + i))
            if b < 4 or i < 3:                 # a regression in the last buckets
                rows.append((oid, "pending", "placed", "", now + b * 60 + i + 1))
    con.executemany("INSERT INTO order_events (order_id, from_state, to_state,"
                    " detail, created_at) VALUES (?,?,?,?,?)", rows)
    con.commit()
    return con


def test_the_funnel_is_bucketed_by_time(tmp_path):
    con = _seeded_db(tmp_path)
    b = LC.funnel_by_bucket(con, bucket_seconds=60.0)
    assert len(b) >= 4
    assert all("place_rate" in x for x in b)


def test_a_regression_in_the_last_buckets_is_detected(tmp_path):
    """The whole objection to a lifetime funnel: a step that collapsed an hour
    ago still reads fine in an aggregate over a week."""
    con = _seeded_db(tmp_path)
    b = LC.funnel_by_bucket(con, bucket_seconds=60.0)
    r = LC.funnel_regression(b, "place_rate", tail=2)
    assert r["enough_data"]
    assert r["delta"] < -0.2, r


def test_the_lifetime_funnel_hides_it(tmp_path):
    con = _seeded_db(tmp_path)
    life = {row["step"]: row for row in LC.funnel(con)}
    # lifetime placed/started is still high, because most history was healthy
    assert life["placed"]["orders"] / life["started"]["orders"] > 0.6


def test_regression_reports_insufficient_data_rather_than_guessing(tmp_path):
    con = _seeded_db(tmp_path)
    b = LC.funnel_by_bucket(con, bucket_seconds=600.0)
    r = LC.funnel_regression(b, "place_rate", tail=3)
    assert r["enough_data"] is False


# --------------------------------------------------------------------------
# the load generator
# --------------------------------------------------------------------------
def test_the_two_modes_are_the_only_two():
    with pytest.raises(ValueError):
        LG.run("read_only_lookup", ("x",), mode="both")


def test_perceived_latency_is_never_less_than_service_latency():
    r = LG.Result(intended_start=1.0, actual_start=1.5, end=2.0, ok=True,
                  outcome="x")
    assert r.perceived_latency >= r.service_latency


def test_summarise_reports_both_latencies_and_the_gap():
    rs = [LG.Result(0.0, 0.1, 0.2, True, "x"), LG.Result(0.1, 0.5, 0.9, True, "x")]
    s = LG.summarise(rs)
    assert s["perceived_p99"] >= s["service_p99"]
    assert s["omission_gap_p99"] >= 0


def test_an_empty_run_summarises_to_nothing_rather_than_dividing_by_zero():
    assert LG.summarise([]) == {}


# --------------------------------------------------------------------------
# the HTTP surface
# --------------------------------------------------------------------------
def _client():
    tc = pytest.importorskip("fastapi.testclient")
    import serve
    return tc.TestClient(serve.app), serve


def test_an_unauthenticated_checkout_is_401():
    client, _ = _client()
    r = client.post("/checkout", json={"lines": [{"sku": "TEE", "qty": 1}],
                                       "idempotency_key": "abcdefgh"})
    assert r.status_code == 401


def test_a_repeat_idempotency_key_returns_the_same_order_not_an_error():
    """A client that gets a 409 on its own retry will retry again."""
    client, _ = _client()
    h = {"Authorization": "Bearer tok-alice"}
    body = {"lines": [{"sku": "TEE", "qty": 1}], "idempotency_key": "key-repeat-1"}
    a = client.post("/checkout", json=body, headers=h)
    b = client.post("/checkout", json=body, headers=h)
    assert a.status_code == 200 and b.status_code == 200
    assert a.json()["order"]["order_id"] == b.json()["order"]["order_id"]


def test_out_of_stock_is_409_not_500():
    """A business outcome the client must handle. A 500 tells it to retry
    something that cannot succeed."""
    client, _ = _client()
    h = {"Authorization": "Bearer tok-alice"}
    r = client.post("/checkout", json={"lines": [{"sku": "PAN", "qty": 9999}],
                                       "idempotency_key": "key-oos-1"}, headers=h)
    assert r.status_code == 409


def test_another_customers_order_is_404_not_403():
    """A 403 confirms the order exists, which is a disclosure when ids are
    guessable."""
    client, _ = _client()
    a = client.post("/checkout",
                    json={"lines": [{"sku": "MUG", "qty": 1}],
                          "idempotency_key": "key-alice-priv"},
                    headers={"Authorization": "Bearer tok-alice"})
    oid = a.json()["order"]["order_id"]
    r = client.get("/orders/%s" % oid,
                   headers={"Authorization": "Bearer tok-bob"})
    assert r.status_code == 404


def test_the_owner_can_read_their_own_order():
    client, _ = _client()
    h = {"Authorization": "Bearer tok-alice"}
    a = client.post("/checkout", json={"lines": [{"sku": "MUG", "qty": 1}],
                                       "idempotency_key": "key-alice-own"},
                    headers=h)
    oid = a.json()["order"]["order_id"]
    assert client.get("/orders/%s" % oid, headers=h).status_code == 200


def test_the_ops_view_requires_an_ops_token():
    client, _ = _client()
    assert client.get("/ops/health",
                      headers={"Authorization": "Bearer tok-alice"}).status_code == 403
    assert client.get("/ops/health",
                      headers={"Authorization": "Bearer tok-ops"}).status_code == 200


def test_an_unknown_sku_is_404():
    client, _ = _client()
    r = client.post("/checkout", json={"lines": [{"sku": "NOPE", "qty": 1}],
                                       "idempotency_key": "key-nosuch"},
                    headers={"Authorization": "Bearer tok-alice"})
    assert r.status_code == 404


def test_a_short_idempotency_key_is_rejected_by_validation():
    client, _ = _client()
    r = client.post("/checkout", json={"lines": [{"sku": "TEE", "qty": 1}],
                                       "idempotency_key": "x"},
                    headers={"Authorization": "Bearer tok-alice"})
    assert r.status_code == 422
