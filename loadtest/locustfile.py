"""Load test for requirement R5.5: 2,000 concurrent learners, p95 teaching step < 300 ms.

Each simulated user signs in, gives consent, then works through questions at a
human-ish pace (answering correctly about 70% of the time), occasionally
reloading their dashboard.

Development server (synthetic learners via dev sign-in):
    locust -f loadtest/locustfile.py --host http://127.0.0.1:8000 \\
        --users 2000 --spawn-rate 50 --run-time 10m --headless --csv results

Staging (SSO only): provision test learners in the IdP, sign each in once, then
put one access token per line in LT_TOKENS_FILE and set CT_API_AUDIENCE on the
service; users authenticate with bearer tokens instead of dev sign-in.
"""

from __future__ import annotations

import itertools
import os
import random

from locust import HttpUser, between, events, task

from cognitive_twin.skills import load_track

TRACK = load_track(os.environ.get("LT_TRACK", "ml-foundations"))
Q = f"?track={TRACK.id}"  # as the web app sends it
_ids = itertools.count(1)
TOKENS = [t.strip() for t in open(os.environ["LT_TOKENS_FILE"])] if os.environ.get(
    "LT_TOKENS_FILE") else []


# Think time: how long a person spends reading and answering a question. The default
# (8-30 s) models real learners; LT_MIN_WAIT=1 LT_MAX_WAIT=4 gives a stress profile.
MIN_WAIT = float(os.environ.get("LT_MIN_WAIT", "8"))
MAX_WAIT = float(os.environ.get("LT_MAX_WAIT", "30"))


class Learner(HttpUser):
    wait_time = between(MIN_WAIT, MAX_WAIT)

    def on_start(self):
        n = next(_ids)
        self.headers = {}
        if TOKENS:
            self.headers["Authorization"] = f"Bearer {TOKENS[(n - 1) % len(TOKENS)]}"
        else:
            r = self.client.post("/auth/dev-login", json={"persona": f"loadtest-{n}"},
                                 name="/auth/dev-login")
            self.headers["X-CSRF-Token"] = r.headers.get("x-csrf-token", "")
        self.client.post("/api/me/consent" + Q, json={"learning": True}, headers=self.headers,
                         name="/api/me/consent")

    @task(10)
    def learn(self):
        with self.client.post("/api/me/next" + Q, json={}, headers=self.headers,
                              name="/api/me/next", catch_response=True) as r:
            if r.status_code != 200:
                r.failure(f"next {r.status_code}")
                return
            d = r.json()
        if d["action"] == "break":
            if d.get("proposed_action") != "done":
                self.client.post("/api/me/break" + Q, headers=self.headers, name="/api/me/break")
            return
        item = TRACK.items[d["item"]["id"]]
        choice = item.answer if random.random() < 0.7 else (item.answer + 1) % 4
        self.client.post("/api/me/answer" + Q, json={"item_id": item.id, "choice": choice},
                         headers=self.headers, name="/api/me/answer")

    @task(1)
    def dashboard(self):
        self.client.get("/api/me" + Q, headers=self.headers, name="/api/me")


@events.quitting.add_listener
def check_slo(environment, **_):
    """Fail the run (non-zero exit) if the R5.5 target is missed."""
    for name in ("/api/me/next", "/api/me/answer"):
        s = environment.stats.get(name, "POST")
        p95 = s.get_response_time_percentile(0.95)
        print(f"{name}: requests={s.num_requests} failures={s.num_failures} p95={p95} ms")
        if s.num_requests and (p95 > 300 or s.fail_ratio > 0.01):
            environment.process_exit_code = 1
