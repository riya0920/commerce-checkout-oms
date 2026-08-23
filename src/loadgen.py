"""A load generator with separate client processes and think time.

THE GAP, AND WHY IT MATTERED MORE THAN IT SOUNDS
-------------------------------------------------
"The load test is a thread pool, not a load generator -- no think-time, no
connection pooling, no separate client process, so p99 includes Python thread
scheduling and should not be quoted as service latency."

Three specific distortions, all in the same direction:

  1. THREADS SHARE THE GIL WITH THE SERVER. Every microsecond a client thread
     spends parsing a response is a microsecond the server cannot run, so the
     measured latency contains the client's own cost. That inflates p99 and makes
     the system look worse than it is.
  2. NO THINK TIME MEANS A CLOSED LOOP AT ZERO DELAY, which is not load, it is a
     benchmark of how fast the machine can spin. Real shoppers pause. A closed
     loop with N threads and no think time holds concurrency at exactly N; add
     think time and concurrency becomes a random variable whose mean is
     N x service_time / (service_time + think_time) -- a completely different
     experiment, and the one that resembles traffic.
  3. COORDINATED OMISSION. When a closed-loop client waits for a slow response
     before issuing the next request, it stops sending during exactly the period
     the system is slow -- so the requests that would have queued behind the stall
     are never issued, and their latency never appears. Every closed-loop
     benchmark understates the tail, and the standard correction is to measure
     against the INTENDED send time rather than the actual one.

WHAT THIS FIXES AND WHAT IT DOES NOT
-------------------------------------
Separate processes remove the GIL contention. Think time makes concurrency a
distribution. Intended-start timing corrects coordinated omission. What remains
uncorrected is that client and server are still on one machine with no network,
so these numbers are a floor on latency and say nothing about a real deployment.
"""
from __future__ import annotations

import multiprocessing as mp
import os
import random
import time
from dataclasses import dataclass


@dataclass
class Result:
    intended_start: float
    actual_start: float
    end: float
    ok: bool
    outcome: str

    @property
    def service_latency(self) -> float:
        """Time the server took. What a server-side APM would report."""
        return self.end - self.actual_start

    @property
    def perceived_latency(self) -> float:
        """Time from when the request SHOULD have been sent.

        The coordinated-omission correction. If the client was blocked when this
        request was due, the wait is part of what a user experienced even though
        no server span covers it.
        """
        return self.end - self.intended_start


def _worker(args):
    """One client process, in one of two modes.

    THE TWO MODES ARE DIFFERENT EXPERIMENTS AND CANNOT BE MIXED.

      closed  -- the client thinks, then sends, then waits for the response,
                 then thinks again. Concurrency is bounded by the client count.
                 There is no arrival schedule: the next request happens when this
                 client is ready, which is why a slow system quietly receives
                 FEWER requests.
      open    -- arrivals follow a fixed schedule decided in advance. A slow
                 response does not delay the next request, so queueing is
                 visible, and the difference between when a request was DUE and
                 when it completed is real latency a user experienced.

    An earlier version ran an arrival schedule AND a think sleep in the same
    loop. That is not a third mode, it is a bug: `intended` advanced by the
    arrival interval while the client also slept for the think time, so the
    schedule fell behind real time by one think per request and the
    "perceived latency" was measuring accumulated scheduling drift. It reported a
    1,432 ms p99 against 0.54 ms of service time, which should have been
    obviously wrong and is exactly the sort of number a benchmark reports with a
    straight face.
    """
    (worker_id, n_requests, arrival_interval, think_mean, mode, fn_name,
     fn_args, seed) = args
    rng = random.Random(seed + worker_id)
    from . import loadgen_targets
    fn = getattr(loadgen_targets, fn_name)
    ctx = loadgen_targets.setup(*fn_args)

    out = []
    t0 = time.perf_counter()
    intended = t0
    for i in range(n_requests):
        if mode == "open":
            intended += (rng.expovariate(1.0 / arrival_interval)
                         if arrival_interval > 0 else 0.0)
            now = time.perf_counter()
            if intended > now:
                time.sleep(intended - now)
        else:
            # Closed loop: the request is "due" when the client becomes free, so
            # intended == actual by construction and the omission gap is zero.
            # That is not the closed loop looking good -- it is the closed loop
            # being unable to see the thing being measured.
            intended = time.perf_counter()

        start = time.perf_counter()
        try:
            ok, outcome = fn(ctx, worker_id, i)
        except Exception as exc:                       # pragma: no cover
            ok, outcome = False, type(exc).__name__
        end = time.perf_counter()
        out.append((intended - t0, start - t0, end - t0, bool(ok), str(outcome)))

        if mode == "closed" and think_mean > 0:
            time.sleep(rng.expovariate(1.0 / think_mean))
    loadgen_targets.teardown(ctx)
    return out


def run(fn_name: str, fn_args: tuple, n_clients: int = 8,
        requests_per_client: int = 40, arrival_interval: float = 0.01,
        think_mean: float = 0.05, mode: str = "open", seed: int = 0) -> list[Result]:
    """Run `n_clients` separate PROCESSES against the target.

    `mode` is "open" (arrival schedule, no think) or "closed" (think, no
    schedule). Passing both is not supported, because it is not a thing.
    """
    if mode not in ("open", "closed"):
        raise ValueError("mode must be 'open' or 'closed'")
    args = [(w, requests_per_client, arrival_interval, think_mean, mode,
             fn_name, fn_args, seed) for w in range(n_clients)]
    ctx = mp.get_context("spawn")
    with ctx.Pool(n_clients) as pool:
        chunks = pool.map(_worker, args)
    out = []
    for ch in chunks:
        for intended, start, end, ok, outcome in ch:
            out.append(Result(intended, start, end, ok, outcome))
    return out


def summarise(results: list[Result]) -> dict:
    """Both latencies, side by side. The gap between them IS the omission."""
    import numpy as np
    if not results:
        return {}
    svc = np.array([r.service_latency for r in results]) * 1000
    per = np.array([r.perceived_latency for r in results]) * 1000
    span = max(r.end for r in results) - min(r.actual_start for r in results)
    return dict(
        n=len(results),
        ok_rate=float(np.mean([r.ok for r in results])),
        throughput_per_s=float(len(results) / span) if span > 0 else float("nan"),
        service_p50=float(np.percentile(svc, 50)),
        service_p95=float(np.percentile(svc, 95)),
        service_p99=float(np.percentile(svc, 99)),
        perceived_p50=float(np.percentile(per, 50)),
        perceived_p95=float(np.percentile(per, 95)),
        perceived_p99=float(np.percentile(per, 99)),
        omission_gap_p99=float(np.percentile(per, 99) - np.percentile(svc, 99)),
    )
