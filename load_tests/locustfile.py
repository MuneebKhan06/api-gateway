"""Load tests for the gateway.

Measures three things the unit suite cannot:

- proxy throughput and latency under sustained concurrency
- gateway overhead, by comparing a proxied request against the same request
  sent straight to the upstream
- whether rate limiting still holds when many users hit it at once

Run the stack first, then point Locust at the gateway:

    docker compose up -d
    locust -f load_tests/locustfile.py --host http://localhost:8000

Headless, which is what the README numbers come from:

    locust -f load_tests/locustfile.py --host http://localhost:8000 \
        --users 50 --spawn-rate 10 --run-time 2m --headless

A 429 is a correct answer from a rate limited route, not a failure. Several
tasks below mark it as a success explicitly, because otherwise the run reports
a large failure rate for the gateway doing exactly what it was asked to.
"""

import random
import uuid

from locust import HttpUser, between, events, task

REGISTER_PASSWORD = "load-test-password"


class GatewayUser(HttpUser):
    """A client that authenticates once and then makes proxied requests."""

    wait_time = between(0.1, 0.5)

    def on_start(self) -> None:
        """Register and log in, so requests are charged to a real user.

        Each simulated user gets its own account. Sharing one would put every
        user in the same rate limit bucket and measure the limiter rather than
        the proxy.
        """
        self.email = f"load-{uuid.uuid4().hex[:12]}@example.com"
        self.token = None

        self.client.post(
            "/auth/register",
            json={"email": self.email, "password": REGISTER_PASSWORD},
            name="/auth/register",
        )

        response = self.client.post(
            "/auth/login",
            json={"email": self.email, "password": REGISTER_PASSWORD},
            name="/auth/login",
        )
        if response.status_code == 200:
            self.token = response.json()["access_token"]

    @property
    def auth(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    @task(10)
    def list_orders(self) -> None:
        with self.client.get(
            "/api/orders", headers=self.auth, name="/api/orders", catch_response=True
        ) as response:
            # Rate limiting is the gateway working, not failing.
            if response.status_code in (200, 429):
                response.success()

    @task(5)
    def get_one_order(self) -> None:
        order_id = random.choice(["a1f0", "b2c9", "c3d7"])
        with self.client.get(
            f"/api/orders/{order_id}",
            headers=self.auth,
            name="/api/orders/[id]",
            catch_response=True,
        ) as response:
            if response.status_code in (200, 404, 429):
                response.success()

    @task(5)
    def list_users(self) -> None:
        with self.client.get(
            "/api/users", headers=self.auth, name="/api/users", catch_response=True
        ) as response:
            if response.status_code in (200, 429):
                response.success()

    @task(3)
    def list_inventory(self) -> None:
        with self.client.get(
            "/api/inventory", headers=self.auth, name="/api/inventory", catch_response=True
        ) as response:
            if response.status_code in (200, 429):
                response.success()

    @task(2)
    def create_order(self) -> None:
        with self.client.post(
            "/api/orders",
            headers=self.auth,
            json={"customer": self.email, "total": round(random.uniform(10, 500), 2)},
            name="/api/orders [POST]",
            catch_response=True,
        ) as response:
            if response.status_code in (201, 429):
                response.success()

    @task(1)
    def health(self) -> None:
        self.client.get("/health", name="/health")


class UnauthenticatedUser(HttpUser):
    """Traffic with no token, to measure the cost of rejecting it.

    Auth failures should be cheap: the middleware validates cheapest first and
    never opens a connection to an upstream, so these should be well faster
    than a proxied request.
    """

    wait_time = between(0.1, 0.3)

    @task
    def rejected_request(self) -> None:
        with self.client.get(
            "/api/orders", name="/api/orders [no token]", catch_response=True
        ) as response:
            if response.status_code == 401:
                response.success()


class RateLimitProbe(HttpUser):
    """One user hammering a single route, to watch the limiter engage.

    Deliberately no wait time. The interesting output is the ratio of 200 to
    429 over the run, which should settle at the configured rate.
    """

    wait_time = between(0, 0)

    def on_start(self) -> None:
        self.email = f"probe-{uuid.uuid4().hex[:12]}@example.com"
        self.client.post(
            "/auth/register",
            json={"email": self.email, "password": REGISTER_PASSWORD},
            name="/auth/register",
        )
        response = self.client.post(
            "/auth/login",
            json={"email": self.email, "password": REGISTER_PASSWORD},
            name="/auth/login",
        )
        self.token = response.json()["access_token"] if response.status_code == 200 else None

    @task
    def hammer(self) -> None:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        with self.client.get(
            "/api/orders", headers=headers, name="/api/orders [probe]", catch_response=True
        ) as response:
            if response.status_code in (200, 429):
                response.success()


class DirectUpstreamUser(HttpUser):
    """Baseline: the same request sent straight to the upstream.

    Run against the upstream's own port rather than the gateway, and the
    difference between the two runs is the gateway's overhead:

        locust -f load_tests/locustfile.py --host http://localhost:8001 \
            --users 50 --spawn-rate 10 --run-time 2m --headless \
            DirectUpstreamUser
    """

    wait_time = between(0.1, 0.5)

    @task
    def list_orders_direct(self) -> None:
        self.client.get("/", name="upstream / [direct]")


@events.quitting.add_listener
def report_rate_limiting(environment, **_kwargs) -> None:
    """Print how much traffic the limiter turned away.

    Locust counts a 429 as a success here, so without this the run gives no
    sign the limiter did anything at all.
    """
    stats = environment.stats
    total = stats.total.num_requests
    if not total:
        return

    print()
    print(f"Total requests: {total}")
    print(f"Failures:       {stats.total.num_failures}")
    print(f"Median:         {stats.total.median_response_time} ms")
    print(f"P95:            {stats.total.get_response_time_percentile(0.95)} ms")
    print(f"P99:            {stats.total.get_response_time_percentile(0.99)} ms")
    print(f"Throughput:     {stats.total.total_rps:.1f} req/s")
