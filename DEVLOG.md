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

## Day 4

Rate limiting. Three algorithms, all of them Lua scripts, plus the middleware
that applies them.

**Why Lua and not Python.** A limiter written as GET, decide, SET is broken
under concurrency and it is worth being precise about how. Two requests arrive
together. Both read a counter at 99 against a limit of 100. Both conclude they
are under. Both write 100. Two requests got through on one slot, and the
window that was supposed to cap at 100 served 101. Under real load that is not
a rare race, it is the normal case. Redis runs a Lua script atomically, so the
read, the decision and the write cannot be interleaved by another request.

**Fixed window is in here because it is wrong.** It is the cheapest of the
three, one integer per client, and it has a boundary flaw that is the entire
justification for the other two. A client limited to 100 per minute sends 100
at 00:00:59 and 100 more at 00:01:00. Both windows are within limit. The
client just sent 200 requests in about a second.

I wrote a test that demonstrates this directly rather than asserting it in
prose, and another that shows sliding window refusing the same burst. If the
README is going to claim fixed window has a boundary problem, there should be
a test that fails if it ever stops having one.

**Token bucket refill is lazy.** Nothing runs on a timer topping up buckets.
Each bucket stores its token count and the timestamp of that count, and the
next request works out what accrued in between. An idle client costs nothing
while idle. Refill is capped at capacity, otherwise a client that goes quiet
for an hour comes back able to send an hour's worth at once.

**Clocks.** Both token bucket and sliding window need to know what time it is,
and I read that from Redis rather than from the gateway process. With more
than one gateway instance, using each process's own clock means their idea of
a client's bucket disagrees by however much their clocks have drifted. Redis
is the one clock every instance already shares.

Also guarded against the clock going backwards. It should not, but a limiter
that credits tokens on a negative elapsed time hands out free requests when it
does.

**A sorted set member has to be unique.** First version of the sliding window
scored entries by timestamp and used the timestamp as the member too. Two
requests in the same millisecond collide on the same member, ZADD overwrites
rather than adds, and the client gets a free request. Member is now timestamp
plus a random suffix.

**Testing caught something I would have shipped.** All the limiter tests
passed on the first run, which was suspicious given I had just written three
Lua scripts. They passed because fakeredis cannot execute Lua without lupa
installed, EVALSHA was raising, and every limiter was taking its fail-open
path. The tests were asserting that a completely broken limiter allows
requests, which it does.

Installed lupa, and added a session fixture that fails the whole run with a
clear message if Lua is unavailable. Fail-open is the right behaviour in
production and a trap in tests, so the trap needed a tripwire.

**entry_count was lying.** It reported ZCARD, which includes entries that have
aged out but not yet been swept, since pruning only happens inside the script
on the next request. Split it into entry_count, which counts what is actually
inside the window, and stored_entry_count, which counts what Redis is holding.
The second one is the honest number for the memory tradeoff and is what the
benchmark will measure.

**Middleware order.** Rate limiting runs after auth, deliberately. An
authenticated request is charged to its user id; only anonymous traffic falls
back to the address. Limiting by address when a user is known would put an
entire office behind one NAT into a single shared bucket.

Also made sure the limiter does not answer for paths that match no route. Same
mistake I nearly made with auth on Day 3: an unknown path would come back 429
instead of 404, which tells a caller the path exists.

**The blacklist hot key.** Every authenticated request was doing a Redis
lookup to check revocation. That is a round trip per request, and every
gateway instance reads the same key space, so it becomes a hot spot. Added a
small bounded TTL cache in front of it. Fifty requests with one token now do
one Redis lookup instead of fifty.

The staleness this introduces is real and bounded: an instance can serve a
cached "not revoked" answer for up to five seconds after the revocation lands
in Redis. That is much smaller than the 15 minute access token lifetime the
blacklist is already bounded by. Two details keep it from being a regression:
a positive answer is cached far longer, since revocation is not undone, and a
Redis failure is never cached, because caching a failure would stretch a brief
blip into seconds of unchecked tokens on every instance.

The cache is bounded at 10,000 entries. An unbounded dict keyed by token id is
a memory leak that any client can drive by sending fresh tokens.

**Route introspection.** /gateway/routes has been reporting a placeholder for
the rate limit since Day 1. It now looks the limiter up in the registry rather
than reading the config, so a route whose limiter failed to build reports no
limit instead of claiming one that is not being applied. There is a test that
sends exactly as many requests as the endpoint advertises and checks that the
next one is the one that gets rejected.

302 tests passing. Tomorrow: the circuit breaker.
