# Hookline

**A reliable webhook delivery service.** Accept an event in single-digit milliseconds, then
deliver it to your customers' HTTP endpoints with retries, exponential backoff, HMAC signatures,
and a permanent audit trail of every attempt.

A smaller, readable implementation of what [Svix](https://svix.com) and
[Hookdeck](https://hookdeck.com) sell as a product.

> **Status:** in active development. Phase 2 of 10 complete — see [Roadmap](#roadmap).

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
docker compose up -d          # start postgres, wait for healthy
uv run alembic upgrade head   # apply migrations
uv run fastapi dev src/hookline/main.py
```

Open <http://127.0.0.1:8000/docs> for the interactive API docs.

Verify it's alive:

```bash
curl -s localhost:8000/health   # {"status":"ok"}  — process is up
curl -s localhost:8000/ready    # {"status":"ok"}  — database reachable
```

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

`/health` and `/ready` are separate on purpose. Kubernetes treats them very differently: a
failing liveness probe restarts the pod, a failing readiness probe just pulls it out of the load
balancer. If liveness checked the database, a brief Postgres blip would restart every pod in the
deployment at once — turning a recoverable outage into a much worse one.

Events, delivery attempts, and replay endpoints arrive in Phases 3–4.

---

## Configuration

All settings are read from the environment with the `HOOKLINE_` prefix, or from a local `.env`
file. Defaults are in [`src/hookline/config.py`](src/hookline/config.py).

| Variable | Default | Meaning |
|---|---|---|
| `HOOKLINE_APP_NAME` | `hookline` | Service name, used in logs |
| `HOOKLINE_DEBUG` | `false` | Echo SQL to stdout |
| `HOOKLINE_DATABASE_URL` | `postgresql+asyncpg://hookline:hookline@localhost:5432/hookline` | Postgres DSN — note the `+asyncpg` driver |
| `HOOKLINE_MAX_DELIVERY_ATTEMPTS` | `5` | Attempts before an event is dead-lettered |
| `HOOKLINE_DELIVERY_TIMEOUT_SECONDS` | `10.0` | Per-request timeout when delivering |

---

## Project layout

```
hookline/
├── compose.yaml                 # postgres for local dev
├── pyproject.toml               # deps + ruff/mypy config
├── uv.lock                      # committed, pinned dependency graph
├── alembic.ini
├── alembic/
│   ├── env.py                   # wires alembic to app settings + metadata
│   └── versions/                # migrations, reviewed by hand
└── src/hookline/
    ├── main.py                  # app factory + lifespan
    ├── config.py                # pydantic-settings
    ├── db/
    │   ├── base.py              # DeclarativeBase + constraint naming convention
    │   └── session.py           # engine, sessionmaker, per-request session
    ├── models/                  # SQLAlchemy ORM tables
    ├── schemas/                 # Pydantic wire models
    ├── repositories/            # data access
    └── api/
        ├── deps.py              # Annotated dependency aliases
        ├── health.py            # liveness / readiness
        └── v1/
            ├── router.py
            └── routes/          # HTTP handlers
```

### Layer discipline

Each layer knows the layer below it and never the one above.

| Layer | Responsibility | Must not know about |
|---|---|---|
| `config.py` | environment → validated settings | HTTP |
| `schemas/` | wire format in and out | storage |
| `models/` | table definitions | HTTP |
| `repositories/` | data access | HTTP, transaction boundaries |
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

---

## Development

```bash
uv run fastapi dev src/hookline/main.py    # dev server with reload
uv run ruff check --fix . && uv run ruff format .
uv run mypy src                            # strict mode, must stay clean
docker compose ps                          # db should report "healthy"
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

---

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 0 | uv, Python 3.13, Postgres via Docker Compose, scaffold | ✅ done |
| 1 | FastAPI core — routers, Pydantic v2, DI, settings, in-memory store | ✅ done |
| 2 | Postgres, SQLAlchemy 2.0 async, Alembic, repository pattern | ✅ done |
| 3 | Events ingest, idempotency keys, delivery attempts table | next |
| 4 | **Delivery worker** — transactional outbox, `SKIP LOCKED`, backoff + jitter, circuit breaker, HMAC signing, DLQ + replay | |
| 5 | Redis — token bucket rate limiting, idempotency store, caching | |
| 6 | Auth — hashed API keys, scopes, incoming signature verification | |
| 7 | Observability — structlog JSON logs, Prometheus, OpenTelemetry, probes | |
| 8 | Testing — pytest, pytest-asyncio, testcontainers, k6 load test | |
| 9 | Dashboard — HTMX + Jinja2, event log with a retry button | |
| 10 | Ship — multi-stage Dockerfile, Helm chart, Minikube, GitHub Actions | |

Phase 4 is the substance of the project. Phases 1–3 are the groundwork that makes it possible.

---

## Tech stack

| | |
|---|---|
| Language | Python 3.13 |
| Web framework | FastAPI, Pydantic v2 |
| Database | PostgreSQL 17, SQLAlchemy 2.0 (async), asyncpg |
| Migrations | Alembic |
| Tooling | uv, ruff, mypy (strict) |
