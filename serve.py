"""The HTTP surface: a storefront, a checkout, and an ops view.

THE GAP
-------
"No HTTP API, no storefront, no auth. Everything is called in-process."

WHAT PUTTING IT BEHIND HTTP ACTUALLY FORCES
--------------------------------------------
Not much about the domain logic -- that was already right. What it forces is the
set of decisions an in-process call never has to make, and each one is a place
this project previously had an implicit answer:

  * IDEMPOTENCY BECOMES THE CLIENT'S PROBLEM AND THEREFORE THE SERVER'S. A retry
    over a network is indistinguishable from a second order. The key is required,
    not optional, and a repeat returns the ORIGINAL order rather than an error --
    because a client that gets a 409 on its own retry will retry again.
  * AUTHENTICATION IS A CUSTOMER IDENTITY, and identity decides what you may
    read. The audit view leaks every order in the system if it does not check.
  * ERROR CODES ARE A CONTRACT. Out-of-stock is 409, not 500: it is a business
    outcome the client must handle, and returning 500 tells the client to retry
    something that will never succeed.

AUTH HERE IS A BEARER TOKEN IN A DICT. That is not a security design and it is
not presented as one -- there is no hashing, no rotation, no expiry, no rate
limiting. What it demonstrates is the AUTHORISATION boundary: that
`/orders/{id}` checks whether this caller owns that order, which is the check
that actually leaks data when it is missing.

Run:  uvicorn serve:app --port 8010
"""
from __future__ import annotations

import html
import os
import sqlite3
import sys
import threading
import time

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from src import checkout as CO      # noqa: E402
from src import db as DB            # noqa: E402
from src import inventory as INV    # noqa: E402
from src import lifecycle as LC     # noqa: E402
from src import oms as OMS          # noqa: E402
from src import policies as POL     # noqa: E402
from src import psp as PSP          # noqa: E402
from src import taxpolicy as TAX    # noqa: E402
from src.money_fmt import fmt       # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "out", "serve.db")

app = FastAPI(title="SE-1 commerce core",
              description="Checkout, returns, exchanges, and an ops view.")

# Demo tokens. A dict, and the docstring says why that is not a security design.
TOKENS = {"tok-alice": "alice", "tok-bob": "bob", "tok-ops": "__ops__"}

CATALOGUE = [
    dict(sku="TEE", name="Cotton tee", unit_price=3000, category="apparel", stock=40),
    dict(sku="MUG", name="Enamel mug", unit_price=1200, category="apparel", stock=25),
    dict(sku="PAN", name="Frying pan", unit_price=4500, category="electronics", stock=15),
    dict(sku="MILK", name="Whole milk", unit_price=449, category="grocery", stock=60),
]


def _boot():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = DB.init(DB_PATH, fresh=True)
    for p in CATALOGUE:
        DB.seed_stock(con, p["sku"], p["stock"], p["unit_price"])
    con.close()


_boot()
PSP_CLIENT = PSP.FakePSP(seed=1)
BY_SKU = {p["sku"]: p for p in CATALOGUE}

# ONE CONNECTION PER THREAD, and this is the first thing HTTP forced that the
# in-process version never had to think about.
#
# The first version of this file held a single module-level connection, which is
# exactly what every in-process caller in this project does and is fine there.
# Behind FastAPI it is not: requests run on a thread pool, and SQLite refuses a
# connection used from a thread other than the one that created it. The failure
# is a hard error rather than corruption, which is lucky -- the same mistake with
# a driver that permits it produces interleaved transactions instead.
#
# Thread-local rather than a pool because SQLite connections are cheap and this
# is not the interesting part of the project. A real service needs a pool with a
# bounded size, because "one connection per request" against Postgres is how a
# service exhausts its database's connection limit under exactly the load it was
# built to handle.
_LOCAL = threading.local()


def conn() -> sqlite3.Connection:
    c = getattr(_LOCAL, "con", None)
    if c is None:
        c = DB.connect(DB_PATH)
        _LOCAL.con = c
    return c


def caller(authorization: str = Header(default="")) -> str:
    """Bearer token -> customer id. Missing or unknown is 401, never a default."""
    tok = authorization.replace("Bearer ", "").strip()
    if tok not in TOKENS:
        raise HTTPException(401, "unknown or missing bearer token")
    return TOKENS[tok]


class Line(BaseModel):
    sku: str
    qty: int = Field(..., gt=0)


class CheckoutRequest(BaseModel):
    lines: list[Line]
    # REQUIRED, not optional. Over a network a retry is indistinguishable from a
    # second order, and making the key optional means the default behaviour is
    # the unsafe one.
    idempotency_key: str = Field(..., min_length=8)
    mechanism: str = "optimistic"
    tier: str = "standard"


@app.get("/health")
def health():
    return {"ok": True, "skus": len(CATALOGUE)}


@app.get("/catalogue")
def catalogue():
    rows = []
    for p in CATALOGUE:
        cur = conn().execute("SELECT on_hand, reserved FROM stock WHERE sku=?",
                          (p["sku"],)).fetchone()
        rows.append(dict(p, on_hand=cur[0], reserved=cur[1],
                         available=cur[0] - cur[1],
                         price=fmt(p["unit_price"])))
    return {"products": rows,
            "note": ("`available` is on_hand minus RESERVED. Add-to-cart does not "
                     "reserve; only checkout-start does -- see the README")}


@app.post("/checkout")
def do_checkout(req: CheckoutRequest, who: str = Depends(caller)):
    """Place an order. Idempotent by key, and out-of-stock is a 409."""
    lines = []
    for ln in req.lines:
        p = BY_SKU.get(ln.sku)
        if p is None:
            raise HTTPException(404, "unknown sku %r" % ln.sku)
        lines.append(dict(sku=ln.sku, qty=ln.qty, unit_price=p["unit_price"]))
    try:
        res = CO.checkout(conn(), PSP_CLIENT, customer_id=who, lines=lines,
                          idempotency_key=req.idempotency_key,
                          mechanism=req.mechanism)
    except CO.CheckoutError as exc:
        raise HTTPException(409, str(exc))

    # THE DOMAIN RETURNS AN OUTCOME; THE TRANSPORT MAPS IT TO A STATUS CODE.
    #
    # `checkout` does not raise on out-of-stock -- it returns a state, which is
    # right for the in-process flash-sale harness that counts states. It is not
    # right over HTTP, where a 200 tells the client the order was placed. The
    # translation belongs here rather than in the domain: the same outcome is a
    # normal return to one caller and a 409 to another, and pushing the status
    # code down into `checkout` would make it care about a protocol it does not
    # otherwise touch.
    FAILED = {
        "abandoned_out_of_stock": (409, "out of stock"),
        "payment_failed": (402, "payment declined"),
    }
    if res.get("state") in FAILED:
        code, msg = FAILED[res["state"]]
        raise HTTPException(code, "%s: %s" % (msg, res.get("reason", "")))

    tax = TAX.cart_tax([
        dict(sku=ln["sku"], category=BY_SKU[ln["sku"]]["category"],
             gross=ln["unit_price"] * ln["qty"], discount=0) for ln in lines])
    return {"order": res, "tax_detail": tax,
            "note": ("per-line tax, adopted from SE-2. A single cart rate would "
                     "have charged %s more by taxing the exempt lines"
                     % fmt(max(tax["overcharge_from_single_rate"], 0)))}


@app.get("/orders/{order_id}")
def get_order(order_id: str, who: str = Depends(caller)):
    """THE AUTHORISATION CHECK. Without it this endpoint reads any order."""
    row = conn().execute("SELECT customer_id FROM orders WHERE order_id=?",
                      (order_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "no such order")
    if who != "__ops__" and row[0] != who:
        # 404 rather than 403 on purpose: a 403 confirms the order exists, which
        # is itself a disclosure when order ids are guessable.
        raise HTTPException(404, "no such order")
    return {"order_id": order_id,
            "financials": OMS.financial_summary(conn(), order_id),
            "audit": OMS.audit_trail(conn(), order_id)}


@app.get("/reprice")
def reprice(quoted_cents: int, current_cents: int, category: str,
            tier: str = "standard"):
    """The repricing policy, per category and per tier rather than global."""
    return POL.reprice_decision(quoted_cents, current_cents, category, tier)


@app.get("/return-quote")
def return_quote(category: str, days_since_delivery: int, full: bool,
                 merch_cents: int, shipping_cents: int):
    """What comes back on a return, including when the answer is nothing."""
    return POL.return_quote(category=category,
                            days_since_delivery=days_since_delivery,
                            is_full_return=full, merch_refund_cents=merch_cents,
                            shipping_paid_cents=shipping_cents)


@app.get("/ops/health")
def ops_health(who: str = Depends(caller)):
    """The on-call view. `capture_unknown_open` is the one to page on."""
    if who != "__ops__":
        raise HTTPException(403, "ops token required")
    return {"health": LC.health(conn()), "invariants": INV.check_invariants(conn()),
            "ledger": OMS.check_ledger_invariants(conn()),
            "note": ("capture_unknown_open is a COUNT, not a rate: each one is a "
                     "customer who may have been charged for an order that does "
                     "not exist")}


@app.get("/", response_class=HTMLResponse)
def storefront():
    rows = "".join(
        "<tr><td><b>%s</b></td><td><code>%s</code></td><td>%s</td>"
        "<td>%s</td><td>%d</td></tr>"
        % (html.escape(p["name"]), p["sku"], p["category"], fmt(p["unit_price"]),
           conn().execute("SELECT on_hand-reserved FROM stock WHERE sku=?",
                       (p["sku"],)).fetchone()[0])
        for p in CATALOGUE)
    return """<!doctype html><meta charset=utf-8><title>SE-1 storefront</title>
<style>
 body{font:15px/1.55 system-ui,sans-serif;max-width:52rem;margin:2rem auto;padding:0 1rem}
 table{border-collapse:collapse;margin:1rem 0}
 td,th{border:1px solid #ddd;padding:.35rem .6rem;text-align:left}
 code{background:#f4f4f4;padding:.1rem .3rem}
 .why{color:#555;font-size:13px}
</style>
<h1>SE-1 &mdash; commerce core</h1>
<table><tr><th>product</th><th>sku</th><th>category</th><th>price</th>
<th>available</th></tr>%s</table>

<p class=why><b>available = on_hand &minus; reserved.</b> Add-to-cart does not
reserve; only checkout-start does. Reserving at add-to-cart is how a cart holds
inventory for an hour and the item shows out of stock to somebody who would have
bought it.</p>

<p class=why>Checkout requires an <code>Idempotency-Key</code>, and it is
<b>required rather than optional</b>: over a network a retry is
indistinguishable from a second order, so an optional key makes the default
behaviour the unsafe one. A repeat returns the original order, not a 409 &mdash;
a client that gets an error on its own retry will retry again.</p>

<p class=why>Out of stock is <b>409</b>, not 500. It is a business outcome the
client must handle; a 500 tells the client to retry something that cannot
succeed. <code>GET /orders/{id}</code> returns <b>404</b> rather than 403 for
somebody else's order, because a 403 confirms the order exists.</p>

<p class=why>Tokens are a dict in the source. That is not a security design and
is not presented as one &mdash; what it demonstrates is the authorisation
boundary. API: <a href="/docs">/docs</a> &middot;
<a href="/catalogue">/catalogue</a>.</p>
""" % rows
