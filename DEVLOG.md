# Devlog

Running notes while building the gateway. Mostly decisions I had to think
about, and things that surprised me.

## Day 1

Goal for today was to get the skeleton in place: package layout, settings,
the route table, an app that boots, and three upstream services to proxy to.
No proxying yet, no auth, no rate limiting.

**Settings.** Went with pydantic-settings so config validation happens at
startup instead of the first time a value is read. `get_settings()` is cached
with `lru_cache` because parsing the environment on every request would be
silly, and because tests can construct `Settings(...)` directly and pass it
into `create_app()` without touching the process environment.

**Route table.** The plan said config file over database, and building it
confirmed that was right: matching a request is now a prefix comparison
against an in-memory list, no IO at all.

Two things I got wrong on the first pass:

1. Naive `path.startswith(prefix)` matches `/api/ordersXYZ` against the
   `/api/orders` route. Fixed by requiring the next character to be a slash
   (or nothing at all). Added a test for it because I would absolutely
   reintroduce this later otherwise.
2. Route order in the YAML file decided which route won, so `/api/orders`
   shadowed `/api/orders/export` depending on how they were listed. Sorting
   by prefix length at load time makes the file order irrelevant.

Reload is deliberately built so the new table is fully parsed and validated
before it replaces the old one. A typo in routes.yaml on a SIGHUP should not
take routing down on a running gateway. `reload()` returns False and logs
instead, and there is a test that sends a broken config at a live table and
checks the old routes still resolve.

**App.** SIGHUP wired to the reload, same idea as Nginx. Guarded it in a
try/except because SIGHUP does not exist on Windows and I do not want a
platform to be the reason the process refuses to start.

`/health` currently reports only route count. It will grow Redis, Postgres
and upstream checks as those get wired in. `/gateway/routes` reports a
breaker state of "closed" or "disabled" as a placeholder until the breaker
registry exists on Day 5.

**Upstreams.** Three small FastAPI services: orders, users, inventory. Each
one has `/_control/fail` and `/_control/latency` endpoints so I can make an
upstream misbehave on demand. That is going to matter a lot for the circuit
breaker work later. Forcing a real container down in a test is slow and
awkward; flipping a boolean is not.

22 tests passing. Tomorrow: the reverse proxy itself and the correlation ID
middleware.

## Day 2

Reverse proxy day. The gateway now actually forwards traffic.

**Correlation IDs first.** Wrote this as raw ASGI middleware instead of
subclassing BaseHTTPMiddleware, and that was not a style choice. Starlette
runs BaseHTTPMiddleware's endpoint call in a separate task, so a ContextVar
set inside the middleware is not visible to the handler. I wanted the request
ID readable from anywhere without threading it through every function
signature, so the ContextVar had to survive into the handler. Raw ASGI keeps
everything in one task and it works.

Client supplied IDs are honoured, which keeps a trace intact if something
upstream of the gateway already started one. But they get validated first:
length capped at 128 and restricted to alphanumerics plus a few separators.
An unvalidated header goes straight into log files, and later into metric
labels, so this is the cheap place to stop log injection.

**The proxy.** Two things I made sure to get right:

Hop-by-hop headers get stripped. Connection, Upgrade, TE, Transfer-Encoding
and friends describe one TCP hop, not the whole message, so forwarding them
corrupts connection handling on the next hop. RFC 9110 has the list.

The body is streamed, not buffered. `StreamingResponse` over
`aiter_raw()` with a BackgroundTask to close the upstream response once the
body is fully written. I got this wrong the first time by closing the
response before the stream was drained, which truncates the body. The
background task is what defers the close to the right moment.

One shared httpx client for the process, not one per request. Building a
client per request throws away the connection pool every time and pays a
fresh handshake on every single proxied call.

**Error mapping.** Upstream failures are not gateway failures and should not
be reported as 500. Timeout maps to 504, connection refused maps to 502, no
matching route maps to 404. Upstream error bodies are passed through
untouched, and gateway generated errors use their own envelope with the
request ID in it, so a client can tell which side said no.

**Testing without ports.** The gateway calls upstreams over httpx, so testing
it normally means binding real ports. Instead I wrote a transport that routes
outbound requests by hostname into the mock upstream ASGI apps in the same
process. The gateway builds a real request and gets a real response, it just
never touches a socket.

That worked immediately for everything except timeouts, which quietly passed
through and returned 200. Obvious in hindsight: an in-process ASGI call has
no socket to go quiet, so nothing enforces a read timeout. Had to implement
it in the mock transport with asyncio.wait_for and raise httpx.ReadTimeout by
hand.

Adding the catch-all proxy route also broke a correlation test that had
registered its own probe endpoint after create_app. The catch-all is
registered last and swallowed it. Rewrote that test against a bare Starlette
app, which is what it should have been anyway since it tests middleware and
not the gateway.

**Health.** /health now probes every distinct upstream concurrently, two
second timeout. Sequential probes would make the check as slow as the sum of
all upstreams, and this endpoint gets polled a lot. A degraded upstream still
returns 200 on purpose: if a load balancer pulled the gateway out of rotation
because one of three services was down, it would take out the two routes that
were still working.

**Docker.** Compose stack with the gateway, three upstreams, Redis and
Postgres. One shared image for all three upstreams with the module picked at
runtime by an env var, since building three near identical images is wasted
time. Redis and Postgres are not used by any code yet, they are there for
tomorrow.

72 tests passing. Tomorrow: Postgres models, JWT issuing and validation, and
the token blacklist.
