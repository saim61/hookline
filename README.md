# Hookline

**A reliable webhook delivery service.** Accept an event in single-digit milliseconds, then
deliver it to your customers' HTTP endpoints with retries, exponential backoff, HMAC signatures,
and a permanent audit trail of every attempt.

A smaller, readable implementation of what [Svix](https://svix.com) and
[Hookdeck](https://hookdeck.com) sell as a product.

> **Status:** all ten phases complete. Events are accepted, deduplicated, fanned out, signed,
> delivered with retries and backoff, rate limited in both directions, and dead-lettered when
> an endpoint stays down — with an authenticated API, an operator dashboard, JSON logs and
> Prometheus metrics, **292 tests at 94% coverage**, and a Helm chart verified on Minikube.
> See [Roadmap](#roadmap) for what each phase covered.

---

## Why this exists

Sending a webhook looks like a one-liner:

```python
httpx.post(customer_url, json=payload)
```

In production it isn't. That line has no answer for any of the following, and every company
that sends webhooks ends up solving all seven:

| Failure mode | What Hookline does about it |
|---|---|
| The receiver is down | Retry on a schedule, with a tracked attempt count |
| The receiver is slow | Accept and queue (`202`), deliver out of band — your API stays fast |
| Retries cause a stampede | Exponential backoff with jitter, plus a per-endpoint circuit breaker |
| The same event is sent twice | Idempotency keys at ingest |
| The receiver can't trust the payload | Per-endpoint HMAC signature over the request body |
| "We never got that webhook" | Persistent log of every delivery attempt, with status and response |
| An endpoint is permanently dead | Dead letter queue after max attempts, replayable by hand |

The interesting engineering is in the delivery worker — a transactional outbox drained with
`SELECT ... FOR UPDATE SKIP LOCKED`, so multiple workers can run concurrently without
double-delivering. The HTTP API in front of it is deliberately boring.

---

## Who it's for

There are two distinct user types, and keeping them straight is essential to reading the code.

**The SaaS company** — Hookline's customer. They call this API. They register their own
customers' URLs and push events into Hookline. Everything under `/api/v1` is designed for them.

**Their customer** — the receiver. They never touch Hookline or know it exists. They just
receive a signed HTTP POST at their URL and verify the signature.

The `endpoints` resource models **the receiver's destination URL**, registered by the SaaS
company.

> **Naming caution.** In this domain, "endpoint" means *a customer's webhook destination URL* —
> not *a FastAPI route handler*. Route modules live under `api/v1/routes/` so the distinction
> stays visible in the file tree.

---

## How it works

Take a fictional e-commerce SaaS, "ShopFlow", with 200 merchants who want order notifications.

**1. ShopFlow registers a merchant's endpoint.**

```http
POST /api/v1/endpoints
{"url": "https://merchant-42.com/hooks", "event_types": ["order.created"]}

201 Created
{"id": "a3f...", "signing_secret": "whsec_xY9...", ...}
```

ShopFlow passes that secret to merchant-42 out of band. **It is returned exactly once, ever** —
subsequent reads of the resource omit it.

**2. An order comes in. ShopFlow ingests an event.**

```http
POST /api/v1/events
{"event_type": "order.created", "payload": {"order_id": 1001, "total": 4500}}

202 Accepted
```

`202`, not `200`. Nothing has been delivered yet — the event was written to the outbox and the
response returned immediately. ShopFlow's checkout path never waits on merchant-42's server.

**3. A background worker claims pending events**, signs each one with the destination's secret,
and POSTs it. A `2xx` marks the event delivered; anything else schedules the next attempt with
backoff.

**4. After five failed attempts** the event lands in the dead letter queue, visible in the
dashboard and replayable on demand.

```
   Ingest API   ──►   Events outbox   ──►   Delivery worker   ──►   Customer endpoint
 (idempotency)        (postgres)         (claim / retry /          (HMAC-signed POST)
                                          backoff / breaker)
                                                  │
                                                  ▼
                                          Dead letter queue
                                         (after max attempts)
```

---

## Quick start

**Requirements:** Python 3.13+, [uv](https://docs.astral.sh/uv/), Docker.

```bash
git clone <repo-url> && cd hookline
cp .env.example .env          # defaults work as-is for local dev
uv sync                       # install dependencies from uv.lock
docker compose up -d          # start postgres + redis, wait for healthy
uv run alembic upgrade head   # apply migrations

# The API requires a key, and minting a key requires a key - so the first one is
# created straight against the database. Copy the value it prints; it is shown once.
uv run hookline-admin create-key --name "local dev"
```

Then run the two processes, in separate terminals:

```bash
uv run fastapi dev src/hookline/main.py   # the API
uv run hookline-worker                    # the delivery worker
```

The API accepts and stores events; the worker delivers them. **Without the worker running,
events are ingested and queued but never sent** — deliveries just sit in `pending`, which is
the correct behaviour and also the first thing to check when a webhook doesn't arrive.

Open <http://127.0.0.1:8000/docs> for the API docs, or
<http://127.0.0.1:8000/dashboard> for the operator dashboard — sign in there with the key you
just created.

Verify it's alive — the probes are deliberately unauthenticated, since a load balancer
has no key:

```bash
curl -s localhost:8000/health   # {"status":"ok"}  — process is up
curl -s localhost:8000/ready    # database + redis status
```

Everything under `/api/v1` needs the key:

```bash
export HL_KEY=hl_...
curl -s localhost:8000/api/v1/endpoints -H "Authorization: Bearer $HL_KEY"
```

For quick local poking, `HOOKLINE_AUTH_ENABLED=false` treats every request as a fully
privileged key. Never do that anywhere real.

If port 5432 is already taken by a local Postgres install, change the mapping in
`compose.yaml` to `"5433:5432"` and update `HOOKLINE_DATABASE_URL` to match.

---

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness. Process is running. Never touches the database. |
| `GET` | `/ready` | Readiness. Returns `503` if Postgres is unreachable. |
| `POST` | `/api/v1/endpoints` | Register a destination URL. Returns `signing_secret` **once**. |
| `GET` | `/api/v1/endpoints` | List destinations. Secrets omitted. |
| `GET` | `/api/v1/endpoints/{id}` | Fetch one destination. Secret omitted. |
| `DELETE` | `/api/v1/endpoints/{id}` | Remove a destination. |
| `POST` | `/api/v1/events` | Ingest an event. Returns `202` and fans out to subscribers. |
| `GET` | `/api/v1/events` | List recent events, newest first. `limit` / `offset`. |
| `GET` | `/api/v1/events/{id}` | Fetch one event with its payload. |
| `GET` | `/api/v1/events/{id}/deliveries` | Per-destination status for one event. |
| `GET` | `/api/v1/deliveries` | Deliveries by `status`. Defaults to `dead` — the DLQ. |
| `GET` | `/api/v1/deliveries/{id}` | One delivery's status, attempt count, next retry time. |
| `GET` | `/api/v1/deliveries/{id}/attempts` | Every HTTP attempt made, with responses. |
| `POST` | `/api/v1/deliveries/{id}/replay` | Requeue a dead delivery with a fresh budget. |
| `POST` | `/api/v1/api-keys` | Mint a key. Returns the key **once**. `admin` only. |
| `GET` | `/api/v1/api-keys` | List keys, without their values. `admin` only. |
| `GET` | `/api/v1/api-keys/{id}` | One key's metadata. `admin` only. |
| `POST` | `/api/v1/api-keys/{id}/revoke` | Deactivate a key, keeping the row. `admin` only. |

`/health` and `/ready` are separate on purpose. Kubernetes treats them very differently: a
failing liveness probe restarts the pod, a failing readiness probe just pulls it out of the load
balancer. If liveness checked the database, a brief Postgres blip would restart every pod in the
deployment at once — turning a recoverable outage into a much worse one.

### Ingesting an event

```bash
curl -X POST localhost:8000/api/v1/events \
  -H 'content-type: application/json' \
  -H 'Idempotency-Key: order-1001-created' \
  -d '{"event_type": "order.created", "payload": {"order_id": 1001, "total": 4500}}'
```

```json
{
  "id": "5d2c...",
  "event_type": "order.created",
  "created_at": "2026-08-18T11:25:43Z",
  "deliveries_scheduled": 2,
  "duplicate": false
}
```

`deliveries_scheduled` is how many registered endpoints subscribe to this event type. **Zero is
a successful ingest, not an error** — the event is stored and nobody is listening. It is
surfaced in the response precisely so a misconfigured integration doesn't fail silently.

`Idempotency-Key` is optional and highly recommended. Replaying a request with the same key
returns the original event and schedules nothing new; the response carries
`Idempotent-Replay: true` so a caller can tell the two apart without diffing bodies. This is
enforced with `INSERT ... ON CONFLICT DO NOTHING`, so it holds under concurrency — twenty
simultaneous requests with one key produce one event and one set of deliveries.

Events, deliveries, and attempts are three separate tables on purpose:

| Table | One row is | Why it's separate |
|---|---|---|
| `events` | what you ingested | store the payload once, not once per destination |
| `deliveries` | one (event × endpoint) pair | **the outbox row a worker claims** — retry state is per destination |
| `delivery_attempts` | one HTTP request | the audit trail: what was sent, what came back, when |

If retry state lived on the event, one endpoint succeeding and another failing would have
nowhere to go. Splitting it is what makes per-destination backoff and a dead letter queue
possible.

### The delivery worker

The worker is a **separate process** from the API:

```bash
uv run hookline-worker
```

They scale on unrelated signals — the API on request rate, the worker on how slow customer
endpoints happen to be — and a worker blocked on a dozen ten-second timeouts must not be able
to add latency to an ingest call. In Kubernetes they are two Deployments with independent
replica counts. Run as many workers as you like; they coordinate through Postgres alone.

Each loop iteration is **claim → deliver → record**, and those three steps deliberately do not
share a transaction:

```sql
SELECT id FROM deliveries
 WHERE status = 'pending' AND next_attempt_at <= now()
 ORDER BY next_attempt_at
 LIMIT :batch
   FOR UPDATE SKIP LOCKED       -- the whole trick
```

`SKIP LOCKED` is what makes this a work queue instead of a contention point. With a plain
`FOR UPDATE`, worker B blocks until worker A commits, so N workers deliver at the throughput of
one. `SKIP LOCKED` tells Postgres to step over rows another transaction is holding and take the
next free ones, so every worker gets a disjoint batch — no Redis, no lock service, no leader
election.

Claiming also flips the rows to `in_flight` and commits immediately. After that they no longer
match `status = 'pending'`, so nobody re-claims them even though the lock is gone while the slow
part runs. Holding a row lock across a ten-second POST is the classic mistake here.

The price of that split is a crash window: a worker killed between claiming and recording leaves
rows `in_flight` that nobody owns. A **reaper** returns anything `in_flight` past
`HOOKLINE_STALE_DELIVERY_TIMEOUT_SECONDS` to `pending`. Without it those rows are invisible
forever — never retried, never dead-lettered — which is the failure mode that quietly turns
"at least once" into "sometimes never".

So the guarantee is **at-least-once, not exactly-once**. A delivery whose response is lost in
transit gets sent again. Receivers deduplicate on the `webhook-id` header, which is the delivery
id and is stable across every retry of that delivery.

### Retry, backoff and the circuit breaker

| Response | What happens |
|---|---|
| `2xx` | `delivered`, terminal |
| `5xx`, timeout, DNS failure, connection refused | retry with backoff |
| `408`, `425`, `429` | retry with backoff |
| any other `4xx` | `dead` immediately, no retries |

Other `4xx` codes say the request itself is wrong — bad path, rejected payload, bad auth — and
sending identical bytes again cannot change the answer. Retrying five times only delays the dead
letter by an hour while burning the receiver's error budget.

Backoff is exponential with **equal jitter**: the delay after failure N is a random value in
`[exp/2, exp]` where `exp = base * 2^(N-1)`, capped. Roughly 5–10s, 10–20s, 20–40s, 40–80s,
80–160s with the default base of 10.

The jitter is not decoration. Without it, an endpoint that goes down takes all of its pending
deliveries with it and stamps them with the *same* retry time. When it comes back up they all
arrive in the same instant and knock it over again — exactly the stampede the backoff was meant
to prevent.

Backoff alone still isn't enough. If an endpoint has been dead a day and a thousand events are
queued for it, the worker burns a slot and a full timeout on every one, starving endpoints that
are actually healthy. So there's a per-endpoint **circuit breaker**:

```
closed ──5 consecutive failures──► open ──cooldown elapsed──► half_open
  ▲                                                              │
  └──────────────── probe succeeds ◄──── one probe request ───────┘
                                              │ probe fails
                                              ▼
                                        open (fresh cooldown)
```

While open, deliveries for that endpoint are rescheduled **without a request being made and
without consuming an attempt** — the endpoint is known to be down, so charging the delivery for
it would be unfair. The half-open probe is the part that matters: it answers "are they back?"
at the cost of exactly one request, instead of either releasing the whole backlog on a timer or
never noticing recovery at all.

The breaker is in-memory, so it is per worker process: three workers hold three independent
views and an endpoint may take up to 3× the threshold in failures before all of them trip.
Nothing is lost — deliveries are still retried — the protection is just weaker than it looks.
Phase 5 moves it into Redis so workers share one view.

### Dead letter queue and replay

```bash
curl 'localhost:8000/api/v1/deliveries?status=dead'          # the DLQ
curl -X POST localhost:8000/api/v1/deliveries/<id>/replay    # try again
```

Replay raises `max_attempts` rather than zeroing `attempt_count`, so attempt numbers keep
increasing and the replayed attempts **append** to the audit trail instead of colliding with the
numbers already there. The record of why a delivery died stays readable next to what happened
when it was retried.

Only `dead` deliveries can be replayed. Replaying a `pending` one would hand it to a second
worker and replaying a `delivered` one would send the customer a duplicate, so both are refused
with `409` rather than silently doing nothing.

### Verifying a signature (receiver side)

Every request carries three headers:

```
webhook-id:         <delivery id, stable across retries>
webhook-timestamp:  <unix seconds>
webhook-signature:  v1,<base64 hmac-sha256>
```

The signed string is `{webhook-id}.{webhook-timestamp}.{raw body}`, keyed on the signing secret
with the `whsec_` prefix stripped. The body is the exact bytes on the wire — never re-serialise
the JSON before verifying, or key ordering will change the digest.

```python
import base64, hashlib, hmac, time


def verify(secret, headers, raw_body, tolerance=300):
    ts = int(headers["webhook-timestamp"])
    if abs(time.time() - ts) > tolerance:  # replay protection
        return False
    signed = f"{headers['webhook-id']}.{ts}.".encode() + raw_body
    key = secret.removeprefix("whsec_").encode()
    expected = "v1," + base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    # constant-time, and any one of the space-separated signatures may match
    return any(
        hmac.compare_digest(expected, got) for got in headers["webhook-signature"].split(" ")
    )
```

Two details that are easy to get wrong and matter:

**Check the timestamp.** Signing the body alone means a captured request stays valid forever —
anyone who records one can replay it indefinitely. Including the timestamp in the signed string
is what makes a stale request detectable.

**Use `compare_digest`, not `==`.** String equality returns as soon as it finds a differing
byte, so how long the comparison takes reveals how much of the prefix was right. That is enough
to walk a forged signature out one byte at a time.

The `v1,` prefix and the space-separated list exist for rotation: during a change a request can
carry both the old and new signature, and a receiver accepting either sees no downtime.

### Request envelope

```json
{
  "id": "<event id>",
  "type": "order.created",
  "created_at": "2026-08-18T11:25:43+00:00",
  "data": { "order_id": 1001, "total": 4500 }
}
```

Redirects are **not** followed. A customer endpoint returning a 302 is nearly always a
misconfiguration, and following it would send a body signed for one host to a different one.

### Authentication

Every `/api/v1` route needs `Authorization: Bearer hl_...`. `/health`, `/ready` and `/docs`
stay open — a load balancer has no credential to present, and a probe that can fail on auth
is a probe that reports the wrong thing.

**Keys are stored as a SHA-256, never in the clear**, so a database dump is not enough to call
the API. Two choices in that sentence deserve a note, because both look wrong at first glance.

*Why not bcrypt or argon2.* Slow hashes exist to make brute force expensive against
**low-entropy** secrets — humans pick `hunter2`, so every guess must cost milliseconds. A key
here is 256 bits from `secrets.token_urlsafe(32)`; no amount of compute enumerates that space.
Meanwhile a slow hash would run on **every authenticated request**, turning a
login-hardening measure into self-inflicted denial of service: argon2 at 100ms per request caps
the API at ten requests per second per core. Fast hash over a high-entropy secret is the correct
trade, and it is what token systems generally do.

*Why unsalted.* Authentication has to find the row **by** the presented key. A per-row salt
would mean hashing the candidate against every stored key in turn — a full table scan on every
request. A unique index on the digest makes it one indexed lookup. Salts defeat precomputation
against weak secrets; there is no rainbow table for 256-bit random strings.

Each key also stores an 11-character `display_prefix` in the clear, so a log line or dashboard
row can identify *which* key it refers to without the key being recoverable from it.

### Scopes

| Scope | Grants |
|---|---|
| `endpoints:read` / `endpoints:write` | list/read vs register/delete destinations |
| `events:read` / `events:write` | read the event log vs ingest |
| `deliveries:read` / `deliveries:write` | read status and attempts vs replay |
| `admin` | everything, including minting keys |

Read and write are separate per resource so a key can hold exactly what it needs. In practice
the checkout service only ingests, so it gets `events:write` and nothing else — and a leak of
that key cannot read customers' signing secrets, delete endpoints, or mint new keys.

`admin` is treated as a wildcard **at check time** rather than expanded into a list at
creation. A key created today therefore covers a scope added next month, which is what an
operator expects from something called admin, and avoids silently under-privileged keys after
every release.

`401` and `403` are kept distinct: `401` means we do not know who you are, `403` means we do and
you may not. Conflating them makes a scope problem look like a credential problem and sends
people hunting for the wrong bug. A revoked or expired key returns `401` — the *same* response
as an unknown key, deliberately, since distinguishing them tells an attacker holding a leaked
key that it was real.

Revoking deactivates the row rather than deleting it. `name` and `last_used_at` survive, so
"what was this key doing before we killed it" stays answerable — which is exactly the question
an incident asks.

`last_used_at` is written **at most once a minute per key**, gated by a Redis `SET NX EX`.
Updating it on every request would add a row write to the hot path purely for a reporting field.

### Signed inbound requests

A key can be created with `require_signed_requests`, which gives it a signing secret and makes
Hookline verify an HMAC over the request body — the same scheme Hookline uses for outbound
deliveries, so one implementation serves both directions.

```bash
uv run hookline-admin create-key --name "checkout" --scopes events:write --signed-requests
```

Requests then need `webhook-id`, `webhook-timestamp` and `webhook-signature` alongside the
bearer key. This buys two things a bearer token alone cannot:

- The signature covers the body, so an intercepted request cannot be **modified** and reused.
- The timestamp is part of the signed string, so a captured request **stops** being valid. A
  bearer token replayed verbatim is accepted forever; a signed request is not.

### Managing keys

`hookline-admin` talks to Postgres directly, which is what breaks the chicken-and-egg problem:
minting a key requires `admin`, which requires a key, so the first one cannot be created over
HTTP. Anyone able to run the CLI already has database credentials, so it grants nothing they did
not already have. It is also the way to revoke a leaked key when the API itself is what is
misbehaving.

```bash
uv run hookline-admin create-key --name "checkout" --scopes events:write --expires-days 90
uv run hookline-admin list-keys
uv run hookline-admin revoke-key <id>
```

### Rate limiting, in both directions

Two token buckets, both in Redis so every replica shares one view:

| | Keyed on | Protects | Default |
|---|---|---|---|
| **Inbound** | caller (client IP; API key from Phase 6) | Hookline | burst 100, 20/s |
| **Outbound** | destination endpoint | the customer's server | burst 20, 10/s |

Token bucket rather than a fixed window, because a fixed window has a boundary problem: with
a 100/minute limit a caller can send 100 at 11:59:59 and 100 more at 12:00:00 and deliver 200
requests in one second while staying inside the rules. A bucket refills continuously, so the
capacity is the burst and the refill rate is the sustained rate, and both hold at every instant.

The whole check is one Lua script. Read-modify-write from Python would race between replicas:
two processes read "1 token left", both decide they may proceed, both write 0, and two requests
go through on one token. It also uses **Redis's clock**, not the caller's — with several
replicas, per-process clocks disagree by whatever their skew is, and a fast replica can hand out
free tokens by claiming more time has passed than really has.

Inbound limiting is attached to writes only. Throttling reads of the dead letter queue while an
operator is working through an incident would be actively unhelpful, and reads are cheap.

The outbound bucket is checked *before* the circuit breaker, deliberately: being over a
destination's rate budget says nothing about that destination's health, so it must not count
toward opening its circuit. Like a breaker skip, a throttled delivery makes no request and
consumes no attempt.

### Everything in Redis fails open

Redis holds only derived state — buckets, breaker counters, cached subscriber lists. None of it
is a source of truth, so every caller is written to keep working without it:

| Redis down | Behaviour |
|---|---|
| Rate limiter | allows the request, flags `degraded` |
| Circuit breaker | attempts the delivery — retry budget and backoff still bound the damage |
| Subscriber cache | reads as a miss, falls through to Postgres |
| `/ready` | still `200`, with `"redis": "degraded"` in the body |

A rate limiter is a protection mechanism, not a correctness one. Rejecting every request when
Redis is unreachable converts a Redis outage into a total outage, which is strictly worse than
briefly serving unlimited traffic. Same logic for the breaker: it exists to protect customer
endpoints from us, and if it is unavailable the right fallback is to deliver.

`/ready` deliberately does **not** fail on Redis. Failing readiness would pull every pod from
the load balancer to protect a feature that is already designed to be optional.

### The subscriber cache

`event_types @> ARRAY['order.created']` runs once per ingested event, which makes it the
highest-frequency query in the system — and it is near-perfectly cacheable, since endpoint
registrations change rarely while events arrive constantly.

Invalidation is **explicit on every endpoint mutation**, with a 30s TTL only as a backstop for a
crash between the write and the delete. TTL alone would mean a newly registered endpoint
silently misses events for up to the TTL, which to whoever just registered it is
indistinguishable from a bug.

The cache cannot cause incorrect behaviour even when stale, because the fan-out insert filters
the ids in the database:

```sql
INSERT INTO deliveries (id, event_id, endpoint_id, max_attempts)
SELECT gen_random_uuid(), :event_id, e.id, :max_attempts
  FROM endpoints e
 WHERE e.id = ANY(:cached_ids) AND e.is_active
    ON CONFLICT DO NOTHING
```

A cached id belonging to a deleted endpoint would otherwise raise a foreign key violation and
turn an ingest into a `500`; a deactivated one would be delivered to anyway. Filtering inside
the statement makes a stale cache an optimisation problem rather than a correctness one.

### Circuit breaker backends

`HOOKLINE_CIRCUIT_BREAKER_BACKEND` is `redis` (default) or `memory`.

With `memory`, each worker holds its own view, so an endpoint can absorb up to N × threshold
failures before every worker has tripped. With `redis`, one failure count is shared — and the
half-open probe slot is claimed with `HSETNX`, so exactly one worker probes a recovering
endpoint. Without that atomic claim, fifty workers reaching half-open together would each send
"one" probe, which is not a probe, it is a small flood.

---

## Configuration

All settings are read from the environment with the `HOOKLINE_` prefix, or from a local `.env`
file. Defaults are in [`src/hookline/config.py`](src/hookline/config.py).

| Variable | Default | Meaning |
|---|---|---|
| `HOOKLINE_APP_NAME` | `hookline` | Service name, used in logs |
| `HOOKLINE_DEBUG` | `false` | Echo SQL to stdout |
| `HOOKLINE_DATABASE_URL` | `postgresql+asyncpg://hookline:hookline@localhost:5432/hookline` | Postgres DSN — note the `+asyncpg` driver |
| `HOOKLINE_MAX_DELIVERY_ATTEMPTS` | `5` | Attempts before a delivery is dead-lettered |
| `HOOKLINE_DELIVERY_TIMEOUT_SECONDS` | `10.0` | Per-request timeout when delivering |
| `HOOKLINE_WORKER_POLL_INTERVAL_SECONDS` | `1.0` | Sleep when a poll finds nothing due |
| `HOOKLINE_WORKER_BATCH_SIZE` | `20` | Deliveries claimed per poll |
| `HOOKLINE_STALE_DELIVERY_TIMEOUT_SECONDS` | `300.0` | When an `in_flight` row is treated as abandoned |
| `HOOKLINE_RETRY_BASE_DELAY_SECONDS` | `10.0` | First retry window |
| `HOOKLINE_RETRY_MAX_DELAY_SECONDS` | `3600.0` | Backoff cap |
| `HOOKLINE_CIRCUIT_BREAKER_FAILURE_THRESHOLD` | `5` | Consecutive failures before an endpoint's circuit opens |
| `HOOKLINE_CIRCUIT_BREAKER_COOLDOWN_SECONDS` | `60.0` | How long it stays open before a probe |
| `HOOKLINE_CIRCUIT_BREAKER_BACKEND` | `redis` | `redis` (shared) or `memory` (per process) |
| `HOOKLINE_REDIS_URL` | `redis://localhost:6379/0` | Redis connection |
| `HOOKLINE_RATE_LIMIT_ENABLED` | `true` | Inbound limiting on write routes |
| `HOOKLINE_RATE_LIMIT_CAPACITY` | `100` | Inbound burst allowance |
| `HOOKLINE_RATE_LIMIT_REFILL_PER_SECOND` | `20.0` | Inbound sustained rate |
| `HOOKLINE_DELIVERY_RATE_LIMIT_ENABLED` | `true` | Outbound limiting, per destination |
| `HOOKLINE_DELIVERY_RATE_LIMIT_CAPACITY` | `20` | Outbound burst per endpoint |
| `HOOKLINE_DELIVERY_RATE_LIMIT_PER_SECOND` | `10.0` | Outbound sustained rate per endpoint |
| `HOOKLINE_SUBSCRIBER_CACHE_TTL_SECONDS` | `30` | Backstop TTL; invalidation is explicit |
| `HOOKLINE_AUTH_ENABLED` | `true` | Set false only for local poking |
| `HOOKLINE_INBOUND_SIGNATURE_TOLERANCE_SECONDS` | `300` | Clock-skew window for signed requests |
| `HOOKLINE_METRICS_ENABLED` | `true` | Serve `/metrics` |
| `HOOKLINE_WORKER_METRICS_PORT` | `9100` | Worker's scrape port |
| `HOOKLINE_OTEL_ENDPOINT` | *(unset)* | OTLP/HTTP collector; tracing off when unset |
| `HOOKLINE_DASHBOARD_ENABLED` | `true` | Serve the operator UI at `/dashboard` |

`MAX_DELIVERY_ATTEMPTS` is snapshotted onto each delivery at fan-out rather than read at retry
time, so lowering it cannot retroactively dead-letter work already queued.

`STALE_DELIVERY_TIMEOUT_SECONDS` must comfortably exceed `DELIVERY_TIMEOUT_SECONDS`. Set it
lower and the reaper will reclaim rows a healthy worker is still working on, and the customer
gets the webhook twice.

Setting `CIRCUIT_BREAKER_FAILURE_THRESHOLD` below `MAX_DELIVERY_ATTEMPTS` is legal but changes
the behaviour for a dead endpoint: the circuit opens before any single delivery exhausts its
budget, so deliveries are skipped (costing no attempt) rather than failing, and they reach the
DLQ over many cooldown cycles instead of promptly. Nothing is lost, but the DLQ fills slowly.

---

## Project layout

```
hookline/
├── compose.yaml                 # postgres for local dev
├── pyproject.toml               # deps + ruff/mypy config
├── uv.lock                      # committed, pinned dependency graph
├── alembic.ini
├── Dockerfile                   # multi-stage, non-root, one image for all three processes
├── deploy/
│   └── helm/hookline/           # api + worker deployments, migration hook, probes
├── .github/workflows/           # lint, test, image build + boot, helm render
├── tests/
│   ├── unit/                     # no database, no network, no sleeping
│   ├── integration/              # real postgres, real redis, fake receiver
│   └── load/                     # k6, run by hand
├── alembic/
│   ├── env.py                   # wires alembic to app settings + metadata
│   └── versions/                # migrations, reviewed by hand
└── src/hookline/
    ├── main.py                  # app factory + lifespan
    ├── config.py                # pydantic-settings
    ├── enums.py                 # domain vocabulary shared by models and schemas
    ├── auth/
    │   ├── keys.py              # generate + hash api keys
    │   ├── scopes.py            # what a key may do
    │   └── dependencies.py      # bearer auth, scope checks, inbound signatures
    ├── admin/
    │   └── __main__.py          # `hookline-admin`, bootstrap and revoke keys
    ├── dashboard/
    │   ├── routes.py            # pages, one handler per view
    │   ├── session.py           # redis-backed cookie sessions
    │   └── deps.py              # viewer resolution, csrf
    ├── templates/               # jinja2, with partials for htmx swaps
    ├── static/                  # one hand-written stylesheet
    ├── observability/
    │   ├── logging.py           # structlog, JSON or console
    │   ├── context.py           # request id contextvar
    │   ├── middleware.py        # request id, access log, http metrics
    │   ├── metrics.py           # prometheus collectors
    │   └── tracing.py           # opentelemetry, opt-in
    ├── cache/
    │   ├── client.py            # redis pool
    │   ├── ratelimit.py         # token bucket, one Lua script
    │   └── subscribers.py       # subscriber list cache + invalidation
    ├── db/
    │   ├── base.py              # DeclarativeBase + constraint naming convention
    │   └── session.py           # engine, sessionmaker, per-request session
    ├── models/                  # SQLAlchemy ORM tables
    ├── schemas/                 # Pydantic wire models
    ├── repositories/            # data access, one table each
    ├── services/                # business logic spanning several repositories
    ├── delivery/                # pure delivery logic, no database
    │   ├── signing.py           # HMAC sign + verify
    │   ├── backoff.py           # exponential + jitter
    │   ├── breaker.py           # per-endpoint circuit breaker
    │   └── client.py            # the signed POST, and what counts as retryable
    ├── worker/
    │   ├── runner.py            # claim -> deliver -> record loop
    │   └── __main__.py          # `hookline-worker` entrypoint, signal handling
    └── api/
        ├── deps.py              # Annotated dependency aliases
        ├── health.py            # liveness / readiness
        └── v1/
            ├── router.py
            └── routes/          # HTTP handlers
```

`delivery/` holds no I/O beyond the one HTTP call and touches no database. Backoff is a pure
function, the breaker takes an injectable clock, and signing is `bytes in, string out` — so all
three can be tested exhaustively without Postgres, a network, or waiting for real time to pass.
`worker/` is the part that has to deal with both.

### Layer discipline

Each layer knows the layer below it and never the one above.

| Layer | Responsibility | Must not know about |
|---|---|---|
| `config.py` | environment → validated settings | HTTP |
| `enums.py` | shared domain vocabulary | everything — it imports nothing |
| `schemas/` | wire format in and out | storage |
| `models/` | table definitions | HTTP |
| `repositories/` | data access, one table each | HTTP, transaction boundaries, business rules |
| `services/` | business logic across repositories | HTTP, transaction boundaries |
| `api/deps.py` | dependency wiring | business logic |
| `api/v1/routes/` | HTTP concerns | how storage works |

This is what let Phase 2 swap an in-memory dict for Postgres without editing a single route
handler — the repository was given the same method signatures as the store it replaced.

Two conventions worth calling out:

**Three schemas per resource, not one.** `EndpointCreate` (input), `EndpointRead` (output), and
`EndpointCreated` (output, plus `signing_secret`). Because `response_model=list[EndpointRead]`
strips unknown fields, the secret cannot leak from a list response even though the handler
returns the full ORM object. It's a security boundary enforced by types rather than by
remembering to delete a key. Stripe and Svix use the same pattern.

**Repositories never commit.** They `flush()` — which emits SQL but leaves the transaction open.
The commit happens in the request-scoped session dependency, so one request is one transaction.
A handler that writes three rows and then raises rolls back all three, and the repository
doesn't need to know that.

**Services exist for logic that spans repositories.** Ingesting an event writes to `events`,
queries `endpoints`, and writes to `deliveries`. That doesn't belong in a route handler, which
should only translate HTTP, nor in a repository, which should own one table and know nothing
about business rules. `EventIngestService` sits between them — and because it doesn't commit
either, the event and all of its delivery rows land in a single transaction. A fan-out that
fails halfway leaves nothing behind for a worker to find.

---

## Development

```bash
uv run fastapi dev src/hookline/main.py    # api, with reload
uv run hookline-worker                     # delivery worker
uv run pytest                              # 292 tests, ~45s
uv run ruff check --fix . && uv run ruff format .
uv run mypy src                            # strict mode, must stay clean
docker compose ps                          # db and redis should report "healthy"
```

Handy while poking at the worker — turn a normally hour-long retry schedule into seconds:

```bash
HOOKLINE_RETRY_BASE_DELAY_SECONDS=1 HOOKLINE_WORKER_POLL_INTERVAL_SECONDS=0.2 \
  uv run hookline-worker
```

### Migrations

```bash
uv run alembic revision --autogenerate -m "describe the change"
uv run ruff check --fix alembic/versions && uv run ruff format alembic/versions
# READ the generated migration before applying it
uv run alembic upgrade head
```

Three things that will bite you:

**Every new model needs an import in `alembic/env.py`.** Alembic can only see tables registered
on `Base.metadata`, and registration happens as an *import side effect*. Miss the import and
autogenerate produces an empty migration with no error. Those imports carry `# noqa: F401`
because linters correctly see them as unused — don't delete them.

**Autogenerate is a first draft, not a result.** It reads a column rename as a drop plus an add,
which silently destroys data. It frequently misses server-default and type changes. It cannot
infer a backfill. Read every migration before applying it.

**Format generated migrations.** Alembic renders each column on one line, which overruns the
100-character limit on any wide table. The `ruff` line above is part of the workflow, not
optional cleanup.

After applying a migration, run autogenerate once more. It should detect nothing. Anything it
reports is drift between the models and the database, and finding it now is much cheaper than
finding it inside an unrelated migration three weeks later.

One drift source worth knowing about: `sa.Enum(..., create_constraint=True)` attaches its CHECK
constraint to the *type* rather than to the table's metadata, where autogenerate cannot see it.
It then finds the constraint in the database, finds no match in the models, and emits a
`drop_constraint` on every future migration — quietly deleting your validation. `deliveries`
therefore uses `create_constraint=False` plus an explicit `CheckConstraint` in `__table_args__`.

---

## Dashboard

An operator UI at <http://127.0.0.1:8000/dashboard>. Sign in with any API key.

| Page | For |
|---|---|
| Overview | queue depth at a glance, and a warning when deliveries are piling up with nothing delivering |
| Events | the event log, filterable by type, showing fan-out counts per event |
| Event detail | the payload, every destination, and every attempt with its response |
| Deliveries | deliveries by status. Defaults to the dead letter queue, with a replay button |
| Endpoints | registered destinations and what they subscribe to |
| API keys | names, prefixes, scopes, last used |

### Server-rendered, progressively enhanced

Jinja2 templates with HTMX layered on top. **Every action is a real link or a real form
submit.** HTMX only changes how the response is applied — swapping one row instead of
reloading the page — so with JavaScript disabled, or the CDN unreachable, the dashboard still
works. That is the reason there is no build step, no bundler, and no second copy of the data
model in JSON for something whose entire job is showing rows from a table.

The mechanism is one line of server logic: a request carrying `HX-Request` gets a fragment,
anything else gets the whole page wrapping the same fragment. Same handler, same context, so
the enhanced and unenhanced paths cannot drift apart. A test asserts that every `hx-post` sits
on a form that also has `method="post"` and an `action`, which is what stops that guarantee
quietly rotting.

### Authentication

A browser cannot send `Authorization: Bearer hl_...` by following a link, so the dashboard
trades a key for a session once, at login.

**The key never reaches the browser.** Redis holds `session id -> key id`; the cookie carries
only an opaque random id. A stolen cookie is therefore one revocable, expiring session rather
than the credential itself — which is the whole reason this is worth 40 lines instead of just
putting the key in the cookie.

The cookie is `HttpOnly` (an XSS bug cannot read it) and `SameSite=Strict` (the browser does
not send it cross-site at all). Set `Secure` too when you put this behind TLS; it is off by
default so the cookie is not silently dropped over plain HTTP on localhost.

The key is **re-read from the database on every request** rather than trusted from the session,
so revoking or expiring a key logs the holder out immediately instead of at the end of an
eight-hour session.

Replay carries a CSRF token bound to the session, on top of `SameSite=Strict`. Two defences
because the token does not depend on the browser honouring a cookie attribute, and requeuing
someone's webhook deliveries is worth the second one.

Scopes are enforced the same way as on the API. A key without `deliveries:write` gets no replay
button **and** a `403` if it posts to the endpoint anyway — hiding a control is a courtesy, not
authorisation.

### Deliberately read-mostly

The dashboard shows things and replays dead deliveries. It cannot register endpoints, mint
keys, or ingest events. Those are API operations, and giving the same actions two
implementations means two places for a validation rule to be wrong. Replay is the exception
because it is the one thing a human does in response to something they just read on the page.

Turn the whole thing off with `HOOKLINE_DASHBOARD_ENABLED=false` for an API-only deployment.

---

## Observability

Three signals, answering three different questions. Metrics say *that* p99 ingest latency
doubled. Logs say *what happened* to one particular delivery. Traces say *which span inside a
slow request* took the time. None substitutes for another.

### Structured logs

JSON by default, one object per line; colourised key=value when `HOOKLINE_DEBUG=true`.

```json
{"event": "attempt", "delivery_id": "5e63...", "endpoint_id": "6283...",
 "event_type": "order.created", "attempt": 2, "max_attempts": 5, "status_code": 503,
 "duration_ms": 41, "result": "retrying", "request_id": "2a63...",
 "service": "hookline-worker", "level": "info", "timestamp": "2026-08-19T10:46:22.266Z"}
```

The point of the shape: `log.info("attempt", status_code=503, delivery_id=...)` is a queryable
object, where `log.info(f"attempt on {id} got 503")` is a sentence someone writes a regex
against a year later. "Every failed delivery to endpoint X in the last hour" becomes a filter
rather than archaeology.

stdlib `logging` is routed through the same pipeline, so uvicorn, SQLAlchemy and httpx come out
in the same format instead of interleaving two conventions in one stream. uvicorn's access log
is switched off - the middleware already emits one structured line per request with timing,
status and request id, and running both doubles the volume while making the useful one harder
to find.

### Request correlation

Every response carries `X-Request-ID`. A caller-supplied one is honoured so an id can span
several services; otherwise one is generated. Either way it is stamped onto **every** log line
emitted while handling that request - including lines from deep inside a repository - so a user
quoting an id from a failed call finds the exact request and everything that happened in it.

That is carried in a `contextvar`, not a parameter. Threading it through five layers purely so
logging can see it would spread an observability concern across every signature. contextvars are
also the async-correct choice: each task gets its own copy, so two concurrent requests never see
each other's id.

The middleware is raw ASGI rather than `BaseHTTPMiddleware`, which matters here.
`BaseHTTPMiddleware` runs the request inside its own anyio task group, and contextvars set there
are invisible to the handler - which would defeat the entire feature.

### Metrics

`/metrics` on the API (port 8000) and on the worker (port 9100, since the worker serves no HTTP
of its own). Both unauthenticated, like the health probes: Prometheus carries no API key, and in
a cluster neither port is routed publicly.

| Metric | Type | Notes |
|---|---|---|
| `hookline_http_requests_total{method,route,status}` | counter | |
| `hookline_http_request_duration_seconds{method,route}` | histogram | buckets from 1ms |
| `hookline_events_ingested_total{duplicate}` | counter | separates idempotent replays |
| `hookline_deliveries_scheduled_total` | counter | fan-out rows created |
| `hookline_subscriber_cache_lookups_total{result}` | counter | cache hit rate |
| `hookline_delivery_attempts_total{outcome,status_class}` | counter | |
| `hookline_delivery_duration_seconds` | histogram | includes timeouts |
| `hookline_worker_delivery_outcomes_total{outcome}` | counter | delivered / retrying / dead / skipped / throttled |
| `hookline_stale_deliveries_reclaimed_total` | counter | should normally stay flat at zero |
| `hookline_deliveries{status}` | gauge | queue depth, sampled from the table |
| `hookline_oldest_pending_delivery_age_seconds` | gauge | how far behind the worker is |

Three deliberate choices, each a common way to get this wrong.

**Routes are labelled by template, never by path.** `route="/api/v1/events/{event_id}"` is one
time series; the concrete path would be one series *per event*, which is how a monitoring system
gets taken down by the thing it was installed to monitor. Unmatched 404s collapse to
`route="unmatched"` for the same reason. The full prefix is reconstructed on the way out, because
the matched route only knows its path within its own router - using it raw would drop `/api/v1`
and make a future v2 collide with v1.

**Attempts are labelled by status class, not status code.** `5xx` is one series; nobody alerts on
the difference between 502 and 503.

**Queue depth is sampled at scrape time**, because it describes the *table*, not the process - a
delivery created by replica A has to appear in replica B's numbers too. That means every replica
reports the same value, so **aggregate it with `max()`, never `sum()`**. Summing across three
replicas turns a queue of 40 into a graph showing 120 and an alert nobody trusts. Zeroes are
reported explicitly rather than omitted, because PromQL treats an absent series very differently
from one holding 0.

Useful starting queries:

```promql
# ingest latency, p99
histogram_quantile(0.99, sum by (le) (
  rate(hookline_http_request_duration_seconds_bucket{route="/api/v1/events"}[5m])))

# delivery success rate
sum(rate(hookline_delivery_attempts_total{outcome="success"}[5m]))
  / sum(rate(hookline_delivery_attempts_total[5m]))

# the queue is falling behind - note max(), not sum()
max(hookline_oldest_pending_delivery_age_seconds) > 300

# something is crash-looping mid-delivery
rate(hookline_stale_deliveries_reclaimed_total[15m]) > 0
```

### Tracing

Off unless `HOOKLINE_OTEL_ENDPOINT` points at an OTLP/HTTP collector:

```bash
HOOKLINE_OTEL_ENDPOINT=http://localhost:4318/v1/traces uv run fastapi dev src/hookline/main.py
```

FastAPI, SQLAlchemy and httpx are auto-instrumented, so one trace shows the request, the queries
inside it, and the outbound POST to the customer endpoint. Health and metrics URLs are excluded -
they would be the overwhelming majority of spans and are never the thing under investigation.

Opt-in on purpose: an SDK exporting nowhere still costs per-span allocation on every request, and
silently dropping spans is worse than not tracing, because it looks instrumented and is not.
Spans go through `BatchSpanProcessor`, not `SimpleSpanProcessor` - the simple one exports
synchronously on span end, putting a network round trip to the collector inside the request it is
supposed to be measuring.

---

## Testing

```bash
uv run pytest                      # 292 tests, ~45s
uv run pytest tests/unit           # no database, no network, instant
uv run pytest --cov --cov-report=term-missing
```

Two layers, split by what they need rather than by what they are called:

**`tests/unit/`** — backoff, the circuit breaker state machine, signing, key hashing, scope
resolution, schema validation, and the delivery client's retryability rules. No database, no
Redis, no network, no sleeping. The circuit breaker takes an injected clock and backoff takes an
injected `Random`, which is the reason a 60-second cooldown and a jitter distribution can both be
tested exhaustively in milliseconds.

**`tests/integration/`** — the API over ASGI, the repositories against real Postgres, the rate
limiter and shared breaker against real Redis, and the worker delivering to a fake receiver that
verifies the HMAC on arrival. Signing is therefore checked from the *receiver's* side, not only
against itself.

### Infrastructure

By default the suite reuses the compose services on a dedicated database (`hookline_test`) and
Redis db index 15, so a test run can never truncate the table you were about to demo from. Set
`HOOKLINE_TEST_CONTAINERS=1` to start throwaway Postgres and Redis containers instead — slower,
but needs nothing running, which is what CI wants. Both paths are verified.

Schema comes from running the real `alembic upgrade head`, not `Base.metadata.create_all`. That
means a broken migration fails the suite, which the shortcut would quietly hide.

### Things worth knowing if you touch the fixtures

**One event loop for the whole session.** The engine and Redis pools are process-wide singletons
bound to the loop that created them. With a loop per test, an engine built in test A gets
disposed against a loop that no longer exists — which surfaces on Windows as asyncpg writing to a
closed proactor, a long way from the actual cause.

**State is reset by `TRUNCATE`, not by rolling back a transaction.** The code under test manages
its own transactions and commits, and the worker uses several sessions per delivery, so wrapping
a test in one outer transaction does not survive contact with it.

**The worker is driven with `run_once()`, never `run_forever()`.** A polling loop in the
background makes tests race a timer, and those flakes get "fixed" with sleeps until the suite is
slow and still flaky. Driving each batch explicitly means every assertion runs at a known point
in the lifecycle.

**Environment is set at the top of `conftest.py`, before any `hookline` import.** `get_settings()`
is `lru_cache`d, so the first call decides the database URL for the whole process — by the time a
fixture body runs it is far too late.

### Coverage, and a number that was lying

94% of `src/hookline`, and it only became true after adding:

```toml
[tool.coverage.run]
concurrency = ["greenlet", "thread"]
```

SQLAlchemy's asyncio layer runs the synchronous core inside a greenlet, and coverage's tracer
does not follow greenlet switches on its own. Without that line, any code path that awaits a
query is reported as unexecuted. `repositories/event.py` sat at 66% with the idempotency conflict
branch marked uncovered — while a test asserting `duplicate is True` was passing, which is only
reachable *through* that branch. This is worse than having no coverage number, because it sends
you writing tests for paths that are already covered while real gaps stay hidden.

The three entrypoint and tracing modules are excluded rather than padded: `hookline-admin` and
`hookline-worker` are exercised by running them, and tracing needs a live collector to mean
anything.

### Load test

k6, in `tests/load/`. Not run by pytest — it takes minutes and needs a real server. It measures
one thing: that ingest latency stays flat as the delivery backlog grows. If p95 climbs with queue
depth, something has coupled ingest to delivery, which is the whole point of the outbox. See
[tests/load/README.md](tests/load/README.md).

---

## Running it for real

Three processes from one image, differing only by their command: the API, the worker, and a
migration job. Full detail in [deploy/README.md](deploy/README.md); the short version:

```bash
docker compose up -d                                          # whole stack
docker compose exec api hookline-admin create-key --name local
```

```bash
helm upgrade --install hookline deploy/helm/hookline -n hookline --create-namespace \
  --set image.tag=v0.1.0 --set secrets.existingSecret=hookline-credentials
```

The chart declares **no** Postgres or Redis dependency. Bundling them as subcharts makes a chart
that is easy to demo and wrong to run — a database managed by the same release as the
application gets deleted along with it.

Three deployment decisions that are more interesting than the YAML:

**Migrations are a Helm `pre-upgrade` hook**, not an init container (those run once per pod, so
three replicas race three `alembic upgrade` processes on the version table) and not a
post-install hook (with `--wait`, Helm waits for pods that are waiting for the schema — that
deadlocks). Readiness cannot cover for it either: `/ready` runs `select 1`, which succeeds
against an empty schema.

**Workers scale on queue age, not CPU.** A worker waiting on a slow customer endpoint uses
almost no CPU, so a CPU-based HPA barely reacts to the thing that matters. The signal is
`max(hookline_oldest_pending_delivery_age_seconds)`; add replicas and they coordinate through
Postgres with `SKIP LOCKED`, needing no configuration and no leader election.

**Both Deployments carry a `checksum/config` annotation.** Kubernetes restarts nothing when a
ConfigMap changes, and environment variables are read once at startup — without it a config
change appears to apply and quietly does nothing.

Verified on Minikube, not just rendered: all four migrations applied from an empty database, a
`helm upgrade` re-ran the hook as a no-op and rolled the pods with the new config, and **two
worker pods claimed disjoint batches** — six events split 1 and 5, no duplicate delivery. That
last one is `SKIP LOCKED` working across separate processes, which no single-process test can
show.

### CI

Four jobs on every push and pull request: lint (ruff, format, mypy), tests against Postgres and
Redis service containers, a Docker build, and a Helm render.

Two of those do more than the name suggests. The image job **boots the container** and waits for
`/health`, which catches a missing template directory or an entrypoint that is not on `PATH` —
things a successful build cannot detect. It then asserts `/ready` returns 503 with no database,
which is a regression test for the probe split: if `/health` ever starts touching Postgres,
every replica restarts together during the next blip, and this is the check that notices. The
Helm job renders with defaults *and* with every optional feature enabled, because the ingress,
HPA and ServiceMonitor templates are never parsed while their feature is off, then validates the
output with `kubeconform` — `helm template` only proves the Go templates render, not that the
result is a valid Kubernetes object.

---

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 0 | uv, Python 3.13, Postgres via Docker Compose, scaffold | ✅ done |
| 1 | FastAPI core — routers, Pydantic v2, DI, settings, in-memory store | ✅ done |
| 2 | Postgres, SQLAlchemy 2.0 async, Alembic, repository pattern | ✅ done |
| 3 | Events ingest, idempotency keys, fan-out, delivery + attempt tables | ✅ done |
| 4 | **Delivery worker** — transactional outbox, `SKIP LOCKED`, backoff + jitter, circuit breaker, HMAC signing, DLQ + replay | ✅ done |
| 5 | Redis — token bucket rate limiting, shared circuit breaker state, subscriber cache | ✅ done |
| 6 | Auth — hashed API keys, scopes, inbound signature verification | ✅ done |
| 7 | Observability — structlog JSON logs, Prometheus, OpenTelemetry, probes | ✅ done |
| 8 | Testing — pytest, pytest-asyncio, testcontainers, k6 load test | ✅ done |
| 9 | Dashboard — HTMX + Jinja2, event log with a retry button | next |
| 10 | Ship — multi-stage Dockerfile, Helm chart, Minikube, GitHub Actions | |

Phase 4 was the substance of the project; phases 1–3 were the groundwork that made it possible,
and 5–10 are hardening rather than new capability.

Optional Phase 11, if this turns toward AI systems work: pgvector semantic search over event
payloads.

Known gaps, deliberately deferred:

- **No per-tenant isolation.** Every key sees every endpoint, event and delivery. Scopes limit
  *what* a key can do, not *which rows* it can see. Multi-tenancy is not on the roadmap.
- **The CI workflows have never run.** They are written against GitHub Actions and were checked
  by hand locally — lint, tests, the Docker build and boot, `helm lint`, and both `helm
  template` renders — but no push has exercised them on a real runner.
- **No horizontal scaling signal for the worker out of the box.** The chart ships a CPU-based
  HPA, which is a poor proxy; the honest metric needs a custom-metrics adapter or KEDA.

---

## Tech stack

| | |
|---|---|
| Language | Python 3.13 |
| Web framework | FastAPI, Pydantic v2 |
| Database | PostgreSQL 17, SQLAlchemy 2.0 (async), asyncpg |
| Cache / limits | Redis 8, redis-py asyncio, Lua for atomic operations |
| Dashboard | Jinja2 + HTMX, server-rendered, no build step |
| Observability | structlog, prometheus-client, OpenTelemetry |
| Migrations | Alembic |
| Testing | pytest, pytest-asyncio, testcontainers, k6 |
| Tooling | uv, ruff, mypy (strict) |
| Deployment | Docker, Compose, Helm, GitHub Actions |
