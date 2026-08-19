# Load test

Needs [k6](https://k6.io/docs/get-started/installation/). Not run by `pytest` — it takes
minutes and needs a real server, so it is a deliberate thing you go and do, not part of the
feedback loop.

```bash
docker compose up -d
uv run alembic upgrade head
uv run hookline-admin create-key --name "load test" --scopes events:write
uv run fastapi run src/hookline/main.py            # not `dev` - no reload under load

k6 run -e HL_KEY=hl_... tests/load/ingest.js
```

## What it measures

**Ingest latency under sustained arrival rate.** `POST /api/v1/events` should answer in
single-digit milliseconds no matter how slow the receiving endpoints are, because nothing is
delivered inline — the event is written to the outbox and the response returns.

The signal worth watching: **p95 must stay flat as the delivery backlog grows.** If it climbs
with queue depth, something has coupled ingest to delivery, which is the one thing the
transactional outbox exists to prevent.

It uses `ramping-arrival-rate` rather than a fixed pool of VUs. Open-model load keeps arriving
at the target rate even when responses slow down, which is how a real caller behaves. A closed
model quietly reduces its own load when the server struggles, and so hides exactly the problem
you are looking for.

`429`s are counted separately and are **not** failures — the rate limiter returning 429 with a
`Retry-After` under overload is correct behaviour. Raise `HOOKLINE_RATE_LIMIT_CAPACITY` and
`HOOKLINE_RATE_LIMIT_REFILL_PER_SECOND` if you want to push the database rather than the
limiter.

## What it deliberately does not measure

**Delivery throughput.** That is bounded by other people's servers. A number from a synthetic
receiver tells you how fast the fake receiver is, not how fast Hookline delivers. To reason
about delivery capacity, watch the real signals instead:

```promql
rate(hookline_delivery_attempts_total[1m])
max(hookline_oldest_pending_delivery_age_seconds)
```

If that age is growing, add worker replicas — they coordinate through Postgres with
`SKIP LOCKED`, so scaling out needs no configuration change.

## Reading the output

```
http_req_duration{expected_response:true}  p(95)=8.2ms  p(99)=31ms
hookline_deliveries_scheduled              avg=2.0
hookline_throttled                         0
```

Ingest at 600/s with p95 under 10ms on a laptop running Postgres, Redis, the API and the load
generator together is the expected shape. If `hookline_deliveries_scheduled` is 0, no endpoint
is subscribed to `load.test` and the fan-out path is not being exercised at all — register one
first.
