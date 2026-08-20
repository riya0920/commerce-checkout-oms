"""A simulated payment service provider with configurable failure modes.

The mode that matters is TIMEOUT_AFTER_CAPTURE: the PSP received the capture,
took the customer's money, and then the network died before the response came
back. The caller cannot distinguish this from "the capture never happened", and
that ambiguity is the single most consequential state in commerce software. Get
it wrong one way and you charge twice; get it wrong the other and you ship goods
you were never paid for, or refuse an order that is already paid.

"Stripe handles that" is not an answer. The PSP handles ITS side. Your order
state is yours.
"""
from __future__ import annotations

import random
import threading
import uuid


class PSPTimeout(Exception):
    """The response never arrived. The capture may or may not have happened."""


class PSPDeclined(Exception):
    pass


class FakePSP:
    """Thread-safe, deterministic under a seed.

    `ledger` is the PSP's own record -- the thing a reconciliation job would
    query. Crucially it is written BEFORE the timeout is raised, because that is
    what actually happens: the money moves, then the wire breaks.
    """

    def __init__(self, seed: int = 0, timeout_rate: float = 0.0,
                 decline_rate: float = 0.0):
        self._rng = random.Random(seed)
        self._lock = threading.Lock()
        self.timeout_rate = timeout_rate
        self.decline_rate = decline_rate
        self.ledger: dict[str, dict] = {}   # psp_ref -> {order_id, amount, state}
        self.captures_attempted = 0
        self.captures_settled = 0

    def capture(self, order_id: str, amount: int, idempotency_key: str) -> str:
        """Capture funds. Idempotent on `idempotency_key`, like a real PSP."""
        with self._lock:
            for ref, rec in self.ledger.items():
                if rec["idem"] == idempotency_key:
                    return ref                      # replay: same reference
            self.captures_attempted += 1
            roll = self._rng.random()
            ref = "psp_" + uuid.uuid4().hex[:16]

            if roll < self.decline_rate:
                self.ledger[ref] = dict(order_id=order_id, amount=amount,
                                        state="declined", idem=idempotency_key)
                raise PSPDeclined("card declined")

            # The money moves FIRST. Then, maybe, the wire breaks.
            self.ledger[ref] = dict(order_id=order_id, amount=amount,
                                    state="captured", idem=idempotency_key)
            self.captures_settled += 1
            if roll < self.decline_rate + self.timeout_rate:
                raise PSPTimeout("no response after capture sent")
            return ref

    def refund(self, psp_ref: str, amount: int) -> str:
        with self._lock:
            rec = self.ledger.get(psp_ref)
            if rec is None or rec["state"] != "captured":
                raise PSPDeclined("nothing to refund")
            ref = "rfnd_" + uuid.uuid4().hex[:16]
            self.ledger[ref] = dict(order_id=rec["order_id"], amount=amount,
                                    state="refunded", idem=ref)
            return ref

    def void(self, psp_ref: str) -> None:
        with self._lock:
            if psp_ref in self.ledger:
                self.ledger[psp_ref]["state"] = "voided"

    # -- the reconciliation surface --------------------------------------
    def lookup_by_idempotency(self, idempotency_key: str) -> dict | None:
        """What a reconciliation job calls to resolve an ambiguous capture.

        Every PSP offers this. A system that captures without sending an
        idempotency key it can later search on has made the ambiguity
        unresolvable and will eventually double-charge someone.
        """
        with self._lock:
            for ref, rec in self.ledger.items():
                if rec["idem"] == idempotency_key:
                    return dict(rec, psp_ref=ref)
            return None
