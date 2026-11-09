# Load testing (R5.5)

**Target:** 2,000 concurrent learners, with the 95th-percentile response for teaching steps (`/api/me/next`, `/api/me/answer`) under 300 ms.

**Status:** passes at 1,000 learners on a 2-CPU development machine. The 2,000-learner acceptance run must be done on staging, which has production-sized instances. Results go in the table at the end.

## How to run

```bash
pip install -e ".[dev]"
# development server (synthetic learners use development sign-in)
locust -f loadtest/locustfile.py --host http://127.0.0.1:8000 \
  --users 2000 --spawn-rate 25 --run-time 10m --headless --csv results
```

The script exits non-zero if either teaching route misses p95 < 300 ms or fails more than 1% of requests, so it can gate a pipeline. On staging, sign-in is SSO-only: provision test learners in the IdP and pass access tokens with `LT_TOKENS_FILE` (see the docstring in `loadtest/locustfile.py`).

**Learner pacing:** by default each simulated learner spends 8 to 30 seconds per question, as real people do. `LT_MIN_WAIT=1 LT_MAX_WAIT=4` gives a stress profile.

## Results so far

Development machine: 2 vCPUs shared by the server (2 workers), PostgreSQL 16, Redis and the load generator.

| Run | Requests | Failures | Teaching step p50 / p95 / p99 (ms) | Throughput |
|---|---:|---:|---|---:|
| 1,000 learners, 4 min | 25,092 | **0** | next 23 / **260** / 610 · answer 18 / **210** / 640 | 105 req/s |
| 2,000 learners, 5 min | 43,490 | 0.14% | next 2,900 / 5,800 / 7,200 | 145 req/s (CPU-bound) |

At 2,000 learners the shared 2-CPU machine is saturated: latency rises but the service degrades gracefully, with no errors beyond 0.14% and no stalls. Production runs 3 to 10 instances with dedicated CPUs.

## What load testing found and fixed

Load testing was worth it: it found five problems that the functional tests could not.

1. **Cohort creation race.** Many learners from a new cohort signing in at once made duplicate-key errors. Fixed with a savepoint get-or-create; covered by `test_concurrent_first_sign_ins_to_a_new_cohort`.
2. **Connection-pool oversubscription.** 4 workers × 30 connections exceeded PostgreSQL's limit. Pools are now 5 + 5 per process and configurable.
3. **Matrix inversions and BLAS threads on every decision.** Bandit inverses are now cached for the read-only live policy (numerically identical), and BLAS is pinned to one thread per request.
4. **Commit after response.** Writes committed after the response was sent, so a client could act on a response before its write existed. Every route now commits first; `test_writes_are_committed_before_the_response_is_sent` guards it.
5. **Connection leak and deadlock under concurrency.** Handlers released their connection only after FastAPI's post-handler steps, which needed a worker thread. Under bursts this became a circular wait that leaked connections and wedged the service. Sessions now commit inside the handler's own thread, cleanup is shielded from cancellation, the middlewares are pure ASGI, and PostgreSQL ends transactions idle for 30 s as a safety net. `test_cancelled_requests_do_not_leak_connections` guards it.

## Staging acceptance runs

| Date | Instances | Learners | Next p95 | Answer p95 | Failures | Pass? |
|---|---|---:|---:|---:|---:|---|
| `[pending]` | | 2,000 | | | | |
