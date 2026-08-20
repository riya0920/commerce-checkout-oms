"""Schema and connection handling.

SQLite in WAL mode stands in for Postgres. That substitution weakens one specific
claim and the README says so: SQLite serialises writers, so the optimistic-vs-
pessimistic contention benchmark measures the two protocols' round-trip and retry
behaviour, not true row-level concurrency. Everything else here -- reservation
lifecycle, idempotency, capture-unknown reconciliation, the money ledger -- is
unaffected by the substitution.

Money is integer cents throughout. See se2-promo-engine/src/money.py for the
argument; it is the same argument.
"""
from __future__ import annotations

import os
import sqlite3

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=15000;

CREATE TABLE IF NOT EXISTS stock (
    sku          TEXT PRIMARY KEY,
    on_hand      INTEGER NOT NULL CHECK (on_hand >= 0),
    reserved     INTEGER NOT NULL DEFAULT 0 CHECK (reserved >= 0),
    version      INTEGER NOT NULL DEFAULT 0,
    unit_price   INTEGER NOT NULL
);

-- Add-to-cart does NOT create a row here. Only checkout-start does.
CREATE TABLE IF NOT EXISTS reservations (
    reservation_id TEXT PRIMARY KEY,
    order_id       TEXT NOT NULL,
    sku            TEXT NOT NULL REFERENCES stock(sku),
    qty            INTEGER NOT NULL CHECK (qty > 0),
    state          TEXT NOT NULL CHECK (state IN ('held','committed','released')),
    expires_at     REAL NOT NULL,
    created_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_res_state_exp ON reservations(state, expires_at);
CREATE INDEX IF NOT EXISTS ix_res_order ON reservations(order_id);

CREATE TABLE IF NOT EXISTS orders (
    order_id        TEXT PRIMARY KEY,
    state           TEXT NOT NULL,
    idempotency_key TEXT UNIQUE,
    customer_id     TEXT NOT NULL,
    merch_total     INTEGER NOT NULL DEFAULT 0,
    discount_total  INTEGER NOT NULL DEFAULT 0,
    shipping_total  INTEGER NOT NULL DEFAULT 0,
    tax_total       INTEGER NOT NULL DEFAULT 0,
    grand_total     INTEGER NOT NULL DEFAULT 0,
    psp_ref         TEXT,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS order_lines (
    order_id     TEXT NOT NULL REFERENCES orders(order_id),
    line_no      INTEGER NOT NULL,
    sku          TEXT NOT NULL,
    qty          INTEGER NOT NULL CHECK (qty > 0),
    unit_price   INTEGER NOT NULL,
    -- the order-level discount ALLOCATED to this line (se2 does this allocation)
    discount     INTEGER NOT NULL DEFAULT 0,
    qty_shipped  INTEGER NOT NULL DEFAULT 0,
    qty_returned INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (order_id, line_no)
);

-- Append-only. Nothing in this codebase updates or deletes a row here.
CREATE TABLE IF NOT EXISTS money_movements (
    movement_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id     TEXT NOT NULL REFERENCES orders(order_id),
    kind         TEXT NOT NULL CHECK (kind IN ('capture','refund','adjustment')),
    amount       INTEGER NOT NULL CHECK (amount > 0),
    reason       TEXT NOT NULL,
    psp_ref      TEXT,
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_mm_order ON money_movements(order_id);

CREATE TABLE IF NOT EXISTS order_events (
    event_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id   TEXT NOT NULL,
    from_state TEXT,
    to_state   TEXT,
    detail     TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ev_order ON order_events(order_id);

-- Payment attempts, so an ambiguous capture is a ROW rather than a lost thread.
CREATE TABLE IF NOT EXISTS payment_attempts (
    attempt_id   TEXT PRIMARY KEY,
    order_id     TEXT NOT NULL,
    amount       INTEGER NOT NULL,
    state        TEXT NOT NULL CHECK (state IN
                   ('pending','captured','failed','capture_unknown','voided')),
    psp_ref      TEXT,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_pa_state ON payment_attempts(state);
"""


def connect(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path, timeout=15.0, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=15000")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


def init(path: str, fresh: bool = True) -> sqlite3.Connection:
    if fresh:
        for suffix in ("", "-wal", "-shm"):
            p = path + suffix
            if os.path.exists(p):
                os.remove(p)
    con = connect(path)
    con.executescript(SCHEMA)
    return con


def seed_stock(con, sku: str, qty: int, unit_price: int = 1000) -> None:
    con.execute("INSERT OR REPLACE INTO stock(sku,on_hand,reserved,version,unit_price)"
                " VALUES (?,?,0,0,?)", (sku, qty, unit_price))
