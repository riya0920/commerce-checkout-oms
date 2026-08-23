"""Per-line tax, adopted from SE-2 -- and the disagreement that forced it.

THE GAP, AND IT WAS A DISAGREEMENT BETWEEN TWO OF MY OWN PROJECTS
-----------------------------------------------------------------
"Tax is a flat 8.75%. SE-2 now models per-line rates; SE-1 has not adopted them,
so the two projects disagree about tax and SE-1 is the wrong one."

That is worth restating plainly: two services in the same portfolio computed a
different tax on the same cart, and the one that was right had no way to make the
one that was wrong agree. A shared library was already in place for the harder
half of the problem -- `oms.allocate` and `se2/money.allocate` are the same
largest-remainder algorithm, because a partial refund and a per-line tax base
have to agree to the cent -- and the rate table was simply never wired through.

WHY PER-LINE MATTERS AND NOT AS A DETAIL
-----------------------------------------
A cart of taxable apparel, EXEMPT groceries and taxable electronics has no
correct single rate. Applying a blended cart rate taxes the groceries. Applying
the apparel rate to everything taxes them at the wrong rate. There is no cart
number that is right, which is why the order-level discount has to be ALLOCATED
to lines before tax is computable at all -- and that allocation is the same one
the refund path depends on.

THE ORDERING DECISION, STATED
------------------------------
Tax applies to what the customer actually pays, i.e. AFTER a retailer discount,
because a retailer discount reduces the taxable receipt. A MANUFACTURER coupon
generally does not -- the retailer is reimbursed, so the taxable amount is the
pre-coupon price -- and that case is modelled here by a per-line flag rather than
left as prose, because it changes the number and somebody will eventually ask.
"""
from __future__ import annotations

# Product taxability by category, in basis points. This table is the SIMPLE half
# of tax. The hard half -- nexus, destination versus origin sourcing, which
# jurisdiction's rate applies to a shipment crossing state lines -- is a
# different problem and is not modelled; see the README.
DEFAULT_RATES_BP = {
    "apparel": 875,
    "electronics": 875,
    "grocery": 0,          # exempt in most US jurisdictions
    "prepared_food": 875,
    "medical": 0,
    "books": 0,
}
FALLBACK_BP = 875


def rate_for(category: str, rates: dict | None = None) -> int:
    return (rates or DEFAULT_RATES_BP).get(category, FALLBACK_BP)


def line_tax(taxable_cents: int, rate_bp: int) -> int:
    """Half-up rounding at the LINE, which is where a jurisdiction assesses it.

    Rounding once at the cart and allocating the result back to lines produces a
    different total, and the difference is not always a cent -- it is a cent per
    line in the worst case, which on a fifty-line wholesale order is a
    reconciliation break somebody has to explain.
    """
    return (max(taxable_cents, 0) * rate_bp + 5000) // 10000


def cart_tax(lines: list[dict], rates: dict | None = None) -> dict:
    """Tax a cart line by line.

    `lines` items: sku, category, gross (cents), discount (cents, already
    allocated to the line), optional manufacturer_funded (bool).

    Returns the per-line detail plus what a single blended cart rate would have
    charged, because the gap between the two is the entire argument.
    """
    detail, total = [], 0
    gross_sum = disc_sum = 0
    for ln in lines:
        gross = int(ln["gross"])
        disc = int(ln.get("discount", 0))
        # A manufacturer-funded discount does not reduce the taxable receipt:
        # the retailer is reimbursed, so the customer is taxed on the full price.
        base = gross if ln.get("manufacturer_funded") else gross - disc
        bp = rate_for(ln.get("category", ""), rates)
        t = line_tax(base, bp)
        total += t
        gross_sum += gross
        disc_sum += disc
        detail.append(dict(sku=ln.get("sku"), category=ln.get("category"),
                           gross=gross, discount=disc, taxable=base,
                           rate_bp=bp, tax=t,
                           manufacturer_funded=bool(ln.get("manufacturer_funded"))))

    blended = line_tax(gross_sum - disc_sum, FALLBACK_BP)
    return {"lines": detail, "tax_total": total,
            "single_rate_would_charge": blended,
            "overcharge_from_single_rate": blended - total}


def refundable_tax(line: dict, qty_returned: int, qty_ordered: int) -> int:
    """Tax to refund on a partial return: the line's tax, pro rata by quantity.

    Computed from the line's OWN tax rather than the order total, which is the
    only version that is right when the cart mixes exempt and taxable goods --
    refunding order tax pro rata by value would hand back tax on groceries that
    were never taxed.
    """
    if qty_ordered <= 0:
        return 0
    return int(round(line["tax"] * qty_returned / qty_ordered))
