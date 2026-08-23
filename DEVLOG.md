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
