"""Prometheus metrics.

Naming follows the convention: `hookline_<subject>_<unit>`, counters end in `_total`,
durations are in seconds. Boring consistency here is what makes dashboards and alerts
transferable between services.

**Label cardinality is the thing to get right.** Every distinct label combination is a
separate time series held in memory by Prometheus, forever. Labelling by raw request path
would create one series per event id - millions of them - and take the monitoring system
down instead of monitoring it. Routes are therefore labelled with the *template*
(`/api/v1/events/{event_id}`), and status with the code, never with the response body or
an id.
"""

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

# An explicit registry rather than the global default, so tests can build a fresh one and
# a stray import cannot silently register a duplicate collector into shared state.
REGISTRY = CollectorRegistry()


# --------------------------------------------------------------------- http

http_requests = Counter(
    "hookline_http_requests_total",
    "HTTP requests handled.",
    labelnames=("method", "route", "status"),
    registry=REGISTRY,
)

http_request_duration = Histogram(
    "hookline_http_request_duration_seconds",
    "Time to handle an HTTP request.",
    labelnames=("method", "route"),
    # Tuned for an ingest path that should answer in single-digit milliseconds. The
    # default buckets start at 5ms and would put every healthy request in one bucket,
    # making the p50 useless exactly where it matters most.
    buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
    registry=REGISTRY,
)


# --------------------------------------------------------------------- ingest

events_ingested = Counter(
    "hookline_events_ingested_total",
    "Events accepted at the API.",
    labelnames=("duplicate",),
    registry=REGISTRY,
)

deliveries_scheduled = Counter(
    "hookline_deliveries_scheduled_total",
    "Delivery rows created by fan-out.",
    registry=REGISTRY,
)

subscriber_cache_lookups = Counter(
    "hookline_subscriber_cache_lookups_total",
    "Subscriber list lookups, by whether the cache served them.",
    labelnames=("result",),
    registry=REGISTRY,
)


# --------------------------------------------------------------------- delivery

delivery_attempts = Counter(
    "hookline_delivery_attempts_total",
    "HTTP attempts made against customer endpoints.",
    # status_class rather than the exact code: 5xx is one series, not five hundred, and
    # nobody alerts on the difference between 502 and 503.
    labelnames=("outcome", "status_class"),
    registry=REGISTRY,
)

delivery_duration = Histogram(
    "hookline_delivery_duration_seconds",
    "Time spent on one delivery attempt, including a timeout.",
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
    registry=REGISTRY,
)

worker_batches = Counter(
    "hookline_worker_batches_total",
    "Worker poll iterations that claimed at least one delivery.",
    registry=REGISTRY,
)

worker_outcomes = Counter(
    "hookline_worker_delivery_outcomes_total",
    "What happened to each claimed delivery.",
    labelnames=("outcome",),
    registry=REGISTRY,
)

stale_reclaimed = Counter(
    "hookline_stale_deliveries_reclaimed_total",
    "Deliveries returned to pending after a worker went away mid-delivery.",
    registry=REGISTRY,
)


# --------------------------------------------------------------------- queue depth

# Sampled from the database when /metrics is scraped, not incremented in code, because it
# is a property of the table rather than of this process.
#
# Every replica reports the same value, so aggregate with `max()` in PromQL, not `sum()`.
# Summing across replicas is the classic way to turn a queue of 40 into a graph showing
# 120 and an alert nobody trusts.
deliveries_by_status = Gauge(
    "hookline_deliveries",
    "Delivery rows by status. Global value - aggregate with max(), never sum().",
    labelnames=("status",),
    registry=REGISTRY,
)

oldest_pending_age = Gauge(
    "hookline_oldest_pending_delivery_age_seconds",
    "Age of the oldest delivery that is due and still waiting. Global value.",
    registry=REGISTRY,
)


def status_class(status_code: int | None) -> str:
    """`2xx`, `5xx`, or `none` when no response arrived at all."""
    if status_code is None:
        return "none"
    return f"{status_code // 100}xx"
