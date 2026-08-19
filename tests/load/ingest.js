// k6 load test for the ingest path.
//
//   k6 run -e HL_KEY=hl_... tests/load/ingest.js
//
// What this is measuring: `POST /api/v1/events` should answer in single-digit
// milliseconds regardless of how slow the receiving endpoints are, because delivery
// happens out of band. If p95 here climbs with the size of the delivery backlog,
// something has coupled ingest to delivery - which is the one thing the outbox exists to
// prevent.
//
// Deliberately does NOT measure delivery throughput. That is bounded by other people's
// servers, so a number from a load test says more about the fake receiver than about
// Hookline.

import http from 'k6/http';
import { check, fail } from 'k6';
import { Counter, Trend } from 'k6/metrics';

const BASE = __ENV.HL_BASE || 'http://127.0.0.1:8000';
const KEY = __ENV.HL_KEY;
const EVENT_TYPE = __ENV.HL_EVENT_TYPE || 'load.test';

const throttled = new Counter('hookline_throttled');
const fanout = new Trend('hookline_deliveries_scheduled');

export const options = {
  scenarios: {
    // Ramping arrival rate rather than a fixed number of VUs. Open-model load keeps
    // arriving at the target rate even when responses slow down, which is how a real
    // caller behaves; a closed model quietly reduces load when the server struggles and
    // hides exactly the problem being looked for.
    ingest: {
      executor: 'ramping-arrival-rate',
      startRate: 50,
      timeUnit: '1s',
      preAllocatedVUs: 50,
      maxVUs: 500,
      stages: [
        { target: 200, duration: '30s' },
        { target: 200, duration: '1m' },
        { target: 600, duration: '30s' },
        { target: 600, duration: '1m' },
        { target: 0, duration: '10s' },
      ],
    },
  },
  thresholds: {
    // The ingest promise. Generous enough to survive a laptop running Postgres, Redis and
    // the load generator at once.
    'http_req_duration{expected_response:true}': ['p(95)<50', 'p(99)<200'],
    // 429s are a correct response under load, not a failure - so failures are counted
    // separately from throttling.
    checks: ['rate>0.99'],
  },
};

export function setup() {
  if (!KEY) {
    fail('set HL_KEY to an API key with the events:write scope');
  }
  const probe = http.get(`${BASE}/ready`);
  if (probe.status !== 200) {
    fail(`${BASE}/ready returned ${probe.status}; is the API running?`);
  }
  return {};
}

export default function () {
  const payload = JSON.stringify({
    event_type: EVENT_TYPE,
    payload: {
      order_id: Math.floor(Math.random() * 1e9),
      total: 4500,
      // A few hundred bytes, roughly the shape of a real order notification. Testing with
      // `{}` measures the framework rather than the workload.
      items: [
        { sku: 'SKU-0001', qty: 2, price: 1500 },
        { sku: 'SKU-0002', qty: 1, price: 1500 },
      ],
      customer: { id: 'cus_12345', email: 'someone@example.com' },
    },
  });

  const response = http.post(`${BASE}/api/v1/events`, payload, {
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${KEY}`,
    },
    tags: { name: 'POST /api/v1/events' },
  });

  if (response.status === 429) {
    throttled.add(1);
    // A 429 with Retry-After is the rate limiter working. Checked, not counted as a
    // failure.
    check(response, {
      'throttled responses carry Retry-After': (r) => !!r.headers['Retry-After'],
    });
    return;
  }

  const ok = check(response, {
    'accepted with 202': (r) => r.status === 202,
    'body names the event': (r) => {
      try {
        return !!r.json('id');
      } catch {
        return false;
      }
    },
  });

  if (ok) {
    fanout.add(response.json('deliveries_scheduled'));
  }
}
