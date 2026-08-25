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

## Day 3

Auth day. Users, tokens, blacklist, and the middleware that enforces it.

**Environment first.** Finally created the venv and installed everything from
requirements-dev.txt. Should have done this on Day 1 rather than working
against whatever the system Python happened to have.

**passlib is dead, use bcrypt directly.** requirements.txt said
`passlib[bcrypt]` and it blew up immediately: passlib 1.7.4 probes bcrypt for
a `__about__.__version__` attribute that bcrypt 4.1 removed, then falls over
on a length check it should be handling itself. passlib has had no release
since 2020.

The choice was pin bcrypt backwards to keep a dead abstraction alive, or call
bcrypt directly. Went with calling it directly. It cost about fifteen lines
and removed a dependency that is not coming back.

Kept the 72 byte cap as an explicit schema rule rather than truncating
silently. bcrypt ignores anything past 72 bytes, which means two long
passwords sharing a prefix would be interchangeable. Better to refuse the
password than to quietly weaken it.

**Access and refresh tokens.** The `typ` claim is the part that matters. It
would be easy to sign both token types identically and validate them the same
way, and that would mean a refresh token works as an access token, handing a
7 day credential to every request. `decode_access` refuses anything that is
not typed as an access token, and there is a test for it.

Also tested the alg=none forgery explicitly. PyJWT rejects it because the
algorithm list is passed on decode, but that is exactly the kind of thing
that silently regresses if someone later "simplifies" the decode call.

**Refresh rotation.** Refreshing revokes the token that was used. If a
refresh token leaks, it is only good until the real client next refreshes,
and after that the attacker's copy is dead. The database row is what makes
this real: signature validity alone cannot express "already spent".

**The blacklist.** This is the piece that makes logout mean something. A JWT
is valid until it expires, by design. Without a server side record, logout
deletes the token from the client and changes nothing for anyone who copied
it. Now logout writes the jti to Redis with a TTL matching the token's own
expiry, and the middleware checks it.

Decided it fails open. If Redis is unreachable, `contains()` logs an error and
returns False rather than rejecting everything. Failing closed would turn a
Redis blip into a total gateway outage, which is a worse failure than briefly
honouring a token someone logged out of. The exposure is bounded by the 15
minute access token TTL.

**Middleware ordering.** Validation runs cheapest first: route lookup, then
presence of a bearer token, then signature and expiry, and only then the
Redis blacklist lookup. Putting the Redis call earlier would let anyone
generate gateway Redis traffic by sending garbage tokens.

One thing I nearly got wrong: the middleware originally answered 401 for
paths that matched no route at all. That means an unknown URL reports itself
as protected, which leaks which paths exist. Now an unmatched path falls
through to the proxy layer and gets its 404.

**Timing attacks on login.** An unknown email returns without hashing
anything, which makes it measurably faster than a known email with a wrong
password. That difference is enough to enumerate accounts. Now the unknown
email path hashes a throwaway password so both take about the same time.

**Identity forwarding.** The upstream should not have to decode the JWT
again, so the gateway passes the caller as X-Gateway-User-Id, -Email and
-Roles. The part that makes those headers worth anything is that the same
names are stripped from the incoming request first. If a client could set
X-Gateway-User-Id itself, an upstream trusting it would be trivially
bypassable. There are tests that send spoofed values and check they are
replaced.

**SQLite in tests.** The models avoid Postgres specific types so the suite
runs against SQLite with no container. That surfaced one real portability
bug: SQLite has no timezone type and returns naive datetimes even from a
`DateTime(timezone=True)` column, so comparing against an aware `utcnow()`
raised. Added `as_aware_utc()` on the model rather than patching the test,
because the fix belongs at the boundary where the value is read back.

Also had to switch the test database from `:memory:` to a temp file. An
in-memory SQLite database is scoped to one connection, and the app opens its
own, so it kept finding an empty schema.

**Health.** Now reports three states instead of two. `degraded` means an
upstream is down but the gateway still serves every other route. `unhealthy`
means Redis or Postgres is gone, so nothing can be authenticated or rate
limited. Dependency failure outranks upstream failure, otherwise a gateway
that cannot authenticate anyone would report itself as merely degraded.

167 tests passing. Tomorrow: rate limiting, all three algorithms, with Lua
for atomicity.
