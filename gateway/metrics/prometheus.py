"""Prometheus metric definitions.

The RED method drives what is here: Rate, Errors, Duration, per upstream. Those
three answer most of the questions worth asking about a gateway during an
incident, and everything else is supporting detail.

A note on label cardinality, because it is the way metric systems get killed.
Every distinct combination of label values is a separate time series held in
memory. Labels here are all bounded: a fixed set of upstreams, a fixed set of
HTTP methods, a finite set of status codes. Nothing user controlled goes in a
label. In particular the request path is never a label, because a client
hitting random URLs would create unbounded series; the matched route prefix is
used instead, which is bounded by the size of the route table.
"""

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

#: A registry of this gateway's own metrics.
#:
#: Deliberately not the global default registry. The default is process wide
#: and shared with anything else that imports prometheus_client, which makes
#: tests order dependent: counters keep their values between tests and
#: re-registering the same metric name raises. An explicit registry can be
#: rebuilt per test.
REGISTRY = CollectorRegistry()

# Buckets in seconds. Chosen for a gateway rather than taken from the library
# default, which tops out at 10s and is too coarse at the fast end. A proxied
# request that is behaving costs single digit milliseconds of gateway
# overhead, so the interesting resolution is below 100ms, with a long tail out
# to 30s for upstreams that are timing out.
LATENCY_BUCKETS = (
    0.001, 0.005, 0.01, 0.025, 0.05, 0.075,
    0.1, 0.25, 0.5, 0.75,
    1.0, 2.5, 5.0, 10.0, 30.0,
)


# Rate: how many requests, sliced by where they went and how they ended.
requests_total = Counter(
    "gateway_requests_total",
    "Requests handled by the gateway",
    ["upstream", "method", "status_code"],
    registry=REGISTRY,
)

# Errors: kept separate from requests_total rather than derived from it.
# `error_type` says what actually went wrong, which a status code alone does
# not: a 503 from an open breaker and a 503 forwarded from a sick upstream
# need different responses from whoever is on call.
errors_total = Counter(
    "gateway_errors_total",
    "Requests the gateway could not complete normally",
    ["upstream", "method", "error_type"],
    registry=REGISTRY,
)

# Duration: a histogram, not a summary.
#
# Summaries compute quantiles inside the process, which means the numbers from
# three gateway instances cannot be combined: there is no correct way to
# average a p95. Histograms ship bucket counts, and histogram_quantile()
# aggregates them server side across every instance. With more than one
# instance the histogram is the only option that gives a true p95.
request_duration_seconds = Histogram(
    "gateway_request_duration_seconds",
    "End to end time the gateway spent on a request",
    ["upstream", "method"],
    buckets=LATENCY_BUCKETS,
    registry=REGISTRY,
)

# Requests currently being handled. A gauge because it goes both ways.
requests_in_flight = Gauge(
    "gateway_requests_in_flight",
    "Requests currently being processed",
    ["upstream"],
    registry=REGISTRY,
)


# Rate limiter decisions, split by algorithm so the three can be compared in
# production rather than only in the benchmark.
rate_limit_decisions_total = Counter(
    "gateway_rate_limit_decisions_total",
    "Rate limiter verdicts",
    ["algorithm", "route", "decision"],
    registry=REGISTRY,
)

# Authentication outcomes. `reason` distinguishes an expired token from a
# forged one from a revoked one, which are three very different signals: the
# first is normal, the second is an attack, the third means logout is working.
auth_attempts_total = Counter(
    "gateway_auth_attempts_total",
    "Authentication decisions made by the gateway",
    ["result", "reason"],
    registry=REGISTRY,
)


# Breaker state as a number, because Prometheus only stores numbers.
# 0 closed, 1 open, 2 half open. Encoded rather than using a label per state
# so a single query graphs the state over time; a label per state would need
# three series and a max() to read.
circuit_breaker_state = Gauge(
    "gateway_circuit_breaker_state",
    "Circuit breaker state per upstream (0=closed, 1=open, 2=half_open)",
    ["upstream"],
    registry=REGISTRY,
)

# Transitions, which is what alerting actually wants. A gauge shows the
# current state, but a breaker that opens and closes repeatedly looks calm on
# a gauge sampled every 15 seconds and obvious on a counter.
circuit_breaker_transitions_total = Counter(
    "gateway_circuit_breaker_transitions_total",
    "Circuit breaker state changes",
    ["upstream", "to_state"],
    registry=REGISTRY,
)

# Requests refused without ever reaching the upstream.
circuit_breaker_rejections_total = Counter(
    "gateway_circuit_breaker_rejections_total",
    "Requests refused because the breaker was open",
    ["upstream"],
    registry=REGISTRY,
)

STATE_VALUES = {"closed": 0, "open": 1, "half_open": 2}


def observe_breaker_state(upstream: str, state: str) -> None:
    """Set the gauge for one upstream."""
    circuit_breaker_state.labels(upstream=upstream).set(STATE_VALUES.get(state, 0))


def observe_breaker_transition(upstream: str, to_state: str) -> None:
    observe_breaker_state(upstream, to_state)
    circuit_breaker_transitions_total.labels(upstream=upstream, to_state=to_state).inc()


def observe_breaker_rejection(upstream: str) -> None:
    circuit_breaker_rejections_total.labels(upstream=upstream).inc()


def observe_rate_limit(algorithm: str, route: str, allowed: bool) -> None:
    rate_limit_decisions_total.labels(
        algorithm=algorithm,
        route=route,
        decision="allowed" if allowed else "rejected",
    ).inc()


def observe_auth(result: str, reason: str = "none") -> None:
    auth_attempts_total.labels(result=result, reason=reason).inc()


def observe_request(
    upstream: str, method: str, status_code: int, duration_seconds: float
) -> None:
    """Record one completed request against the RED metrics."""
    requests_total.labels(
        upstream=upstream, method=method, status_code=str(status_code)
    ).inc()
    request_duration_seconds.labels(upstream=upstream, method=method).observe(
        duration_seconds
    )


def observe_error(upstream: str, method: str, error_type: str) -> None:
    errors_total.labels(upstream=upstream, method=method, error_type=error_type).inc()


def classify_error(status_code: int, gateway_error: str | None = None) -> str | None:
    """Name the failure behind a status code, or None if it was not one.

    The gateway's own error envelope carries a machine readable code, and when
    it is present it is more precise than the status: `circuit_open` and
    `upstream_unavailable` are both 503 but mean different things.
    """
    if gateway_error:
        return gateway_error
    if status_code >= 500:
        return f"http_{status_code}"
    if status_code == 429:
        return "rate_limited"
    if status_code in (401, 403):
        return "unauthorized"
    return None
