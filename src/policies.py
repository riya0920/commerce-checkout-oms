"""The policies that were global constants: repricing, shipping refunds, exchange
windows -- and forward-looking allocation.

FOUR GAPS, ONE FILE
-------------------
  "The repricing policy is global, not per-category or per-customer-tier."
  "Shipping is never refunded on any return -- stated as policy, with no
   configuration and no full-return case."
  "Exchanges have no window policy and treat a cheaper replacement as a plain net
   refund."
  "Allocation is per-order and myopic: no forward inventory position, no capacity
   reservation, no view of the order queue behind this one."

WHAT THEY HAVE IN COMMON
------------------------
Every one of them was a hard-coded constant standing where a MERCHANT DECISION
belongs. That is the actual defect, and it is more common than any individual
missing feature: a policy expressed as a literal in a function is a policy nobody
can change, argue with, or audit -- and the first time the business wants it
different by category, the answer is a code change and a deploy.

So these are tables. Not because tables are elegant, but because a merchant can
be shown a table.
"""
from __future__ import annotations

from dataclasses import dataclass, field


# --------------------------------------------------------------------------
# repricing
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class RepricingRule:
    """How much of a price rise between cart and checkout to absorb.

    Absorb the SMALLER of a cash cap and a percentage. The percentage binds on
    cheap items and the cap binds on expensive ones, which is what stops a
    tolerance that is sane for a $10 tee from silently absorbing $90 on a laptop.
    """
    cap_cents: int
    cap_bp: int


# Per category and per customer tier. The values are merchant inputs and the
# point of the table is that they are arguable, not that these are correct.
#
# The tier column encodes a real and slightly uncomfortable decision: a loyalty
# programme that absorbs more for high-value customers IS price discrimination by
# tenure. It is legal, it is widespread, and it belongs in a table somebody signed
# off rather than in an if-statement somebody wrote.
REPRICING = {
    ("apparel", "standard"):     RepricingRule(300, 500),
    ("apparel", "gold"):         RepricingRule(800, 1000),
    ("electronics", "standard"): RepricingRule(500, 200),
    ("electronics", "gold"):     RepricingRule(2000, 400),
    ("grocery", "standard"):     RepricingRule(100, 800),
    ("grocery", "gold"):         RepricingRule(200, 1200),
}
DEFAULT_RULE = RepricingRule(300, 500)


def reprice_decision(quoted_cents: int, current_cents: int, category: str,
                     tier: str = "standard") -> dict:
    """unchanged / reduced / honoured / reconfirm, and what is actually charged.

    `reconfirm` is a real outcome and not an error path. Charging more than the
    customer agreed to costs a chargeback, and a chargeback costs more than the
    abandoned cart -- so the expensive branch is the one that looks like a loss.
    """
    rule = REPRICING.get((category, tier), DEFAULT_RULE)
    if current_cents == quoted_cents:
        return dict(decision="unchanged", charge=quoted_cents, tolerance=0,
                    rule=(rule.cap_cents, rule.cap_bp))
    if current_cents < quoted_cents:
        # Always pass a decrease on. Keeping the difference is legal in most
        # places and is the single fastest way to teach customers to screenshot
        # their cart.
        return dict(decision="reduced", charge=current_cents, tolerance=0,
                    rule=(rule.cap_cents, rule.cap_bp))
    rise = current_cents - quoted_cents
    tolerance = min(rule.cap_cents, quoted_cents * rule.cap_bp // 10000)
    if rise <= tolerance:
        return dict(decision="honoured", charge=quoted_cents, tolerance=tolerance,
                    absorbed=rise, rule=(rule.cap_cents, rule.cap_bp))
    return dict(decision="reconfirm", charge=None, tolerance=tolerance,
                rise=rise, rule=(rule.cap_cents, rule.cap_bp))


# --------------------------------------------------------------------------
# returns: shipping, and windows
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ReturnPolicy:
    window_days: int = 30
    # Shipping is refunded on a FULL return and not on a partial one. That is the
    # common retail rule and it has a reason: a partial return still required the
    # parcel, so the shipping was consumed. A full return means the shipment
    # should not have happened.
    refund_shipping_on_full_return: bool = True
    refund_shipping_on_partial: bool = False
    # Restocking applies only outside a grace period, so the fast returns that
    # correlate with a sizing mistake are free and the slow ones are not.
    restocking_bp: int = 0
    grace_days: int = 14


POLICIES = {
    "apparel": ReturnPolicy(window_days=60, restocking_bp=0),
    # Electronics: shorter window and a restocking fee, because an opened box is
    # worth materially less. Same code path, different row.
    "electronics": ReturnPolicy(window_days=30, restocking_bp=1500, grace_days=7),
    "grocery": ReturnPolicy(window_days=7, refund_shipping_on_full_return=False),
}
DEFAULT_RETURN = ReturnPolicy()


def return_quote(*, category: str, days_since_delivery: int, is_full_return: bool,
                 merch_refund_cents: int, shipping_paid_cents: int) -> dict:
    """What comes back, and why -- including when the answer is nothing."""
    p = POLICIES.get(category, DEFAULT_RETURN)
    if days_since_delivery > p.window_days:
        return dict(eligible=False, reason="outside the %d-day window" % p.window_days,
                    refund=0, shipping_refund=0, restocking_fee=0)

    ship = 0
    if is_full_return and p.refund_shipping_on_full_return:
        ship = shipping_paid_cents
    elif (not is_full_return) and p.refund_shipping_on_partial:
        ship = shipping_paid_cents

    fee = 0
    if p.restocking_bp and days_since_delivery > p.grace_days:
        fee = merch_refund_cents * p.restocking_bp // 10000

    return dict(eligible=True, reason="within policy",
                refund=merch_refund_cents - fee + ship,
                merch_refund=merch_refund_cents, shipping_refund=ship,
                restocking_fee=fee, window_days=p.window_days,
                days_since_delivery=days_since_delivery)


def exchange_eligible(days_since_delivery: int, category: str) -> dict:
    p = POLICIES.get(category, DEFAULT_RETURN)
    return dict(eligible=days_since_delivery <= p.window_days,
                window_days=p.window_days)


def exchange_settlement(returned_total_cents: int, replacement_total_cents: int,
                        min_refund_cents: int = 100) -> dict:
    """What money moves on an exchange, including for a CHEAPER replacement.

    A cheaper replacement is not simply a net refund. Below a floor the refund
    costs more to process than it returns -- payment fees, a statement line the
    customer queries, a support contact -- so small differences become store
    credit instead. The floor is a merchant input, and stating it is the point:
    the alternative is a silent rounding that customers notice and support
    cannot explain.
    """
    diff = replacement_total_cents - returned_total_cents
    if diff > 0:
        return dict(movement="capture", amount=diff, credit=0,
                    net=diff, note="customer pays the difference")
    if diff == 0:
        return dict(movement="none", amount=0, credit=0, net=0,
                    note="even exchange moves no money at all")
    owed = -diff
    if owed < min_refund_cents:
        return dict(movement="store_credit", amount=0, credit=owed, net=-owed,
                    note="below the refund floor: issued as credit")
    return dict(movement="refund", amount=owed, credit=0, net=-owed,
                note="customer is refunded the difference")


# --------------------------------------------------------------------------
# forward-looking allocation
# --------------------------------------------------------------------------
@dataclass
class DCPosition:
    """A DC's stock, what is already committed, and what is inbound."""
    name: str
    on_hand: dict = field(default_factory=dict)
    committed: dict = field(default_factory=dict)
    inbound: dict = field(default_factory=dict)      # sku -> (qty, days_away)
    km: float = 0.0
    daily_demand: dict = field(default_factory=dict)

    def available(self, sku: str) -> int:
        return max(0, self.on_hand.get(sku, 0) - self.committed.get(sku, 0))

    def days_of_cover(self, sku: str) -> float:
        d = self.daily_demand.get(sku, 0.0)
        return self.available(sku) / d if d > 0 else float("inf")


def scarcity_penalty_cents(dc: DCPosition, sku: str, qty: int,
                           cover_floor_days: float = 7.0,
                           penalty_per_day: int = 120) -> int:
    """What it costs TOMORROW to ship this unit from this DC today.

    The myopic allocator's defect, priced. Draining the near DC to save a parcel
    today is a cost paid on the next order that needed it, and a scorer with no
    view of the queue behind this one books that cost as zero.

    The penalty is a cover SHORTFALL charge: shipping from a DC that already has
    less than a week of cover costs the merchant something, and shipping from one
    with months of cover costs nothing. It is a heuristic standing in for a real
    forward-position model, and calling it that is the honest description -- the
    real version needs a demand forecast per DC, which is ML-1's job.
    """
    d = dc.daily_demand.get(sku, 0.0)
    if d <= 0:
        return 0
    after = (dc.available(sku) - qty) / d
    if after >= cover_floor_days:
        return 0
    return int(round((cover_floor_days - max(after, 0.0)) * penalty_per_day))


def inbound_relief(dc: DCPosition, sku: str, within_days: float = 7.0) -> int:
    """Units arriving soon enough to matter. A DC that is low today and has a
    truck arriving Thursday is not scarce, and a scarcity penalty that ignores
    inbound stock will route around a DC that was never short."""
    qty, days = dc.inbound.get(sku, (0, 999))
    return int(qty) if days <= within_days else 0
