"""Request correlation and Prometheus metrics."""

import json
import logging
import re
from collections.abc import Iterator
from uuid import uuid4

import httpx
import pytest
import structlog
from fastapi import FastAPI

from hookline.observability import metrics


def parse_exposition(text: str) -> dict[str, float]:
    """Flatten the exposition format into {'name{labels}': value}."""
    out: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        name, _, value = line.rpartition(" ")
        try:
            out[name.strip()] = float(value)
        except ValueError:
            pass
    return out


@pytest.fixture
def captured_logs(app: FastAPI) -> Iterator[list[dict[str, object]]]:
    """Collect the event dicts, not the rendered lines.

    Asserting on rendered text would test the renderer. Asserting on the dict tests the
    fields, which is what a log query actually consumes.

    Not `structlog.testing.capture_logs()`: production runs with
    `cache_logger_on_first_use=True`, and a cached logger keeps its original processor
    chain, so reconfiguring structlog mid-process captures nothing. Attaching a stdlib
    handler works because our pipeline routes everything through
    `ProcessorFormatter.wrap_for_formatter`, which leaves the event dict on `record.msg`.

    Depends on `app` so it is installed *after* `create_app()` has called
    `configure_logging()`, which clears the root handlers.
    """
    collected: list[dict[str, object]] = []

    class Sink(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if isinstance(record.msg, dict):
                collected.append(record.msg)

    handler = Sink(level=logging.DEBUG)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield collected
    finally:
        root.removeHandler(handler)


class TestRequestId:
    async def test_generated_and_returned(self, api: httpx.AsyncClient) -> None:
        response = await api.get("/api/v1/endpoints")
        assert len(response.headers["x-request-id"]) == 32

    async def test_caller_supplied_id_is_honoured(self, api: httpx.AsyncClient) -> None:
        """So one id can span several services in a distributed trace."""
        supplied = f"caller-{uuid4().hex[:8]}"
        response = await api.get("/api/v1/endpoints", headers={"X-Request-ID": supplied})
        assert response.headers["x-request-id"] == supplied

    async def test_ids_differ_between_requests(self, api: httpx.AsyncClient) -> None:
        first = (await api.get("/api/v1/endpoints")).headers["x-request-id"]
        second = (await api.get("/api/v1/endpoints")).headers["x-request-id"]
        assert first != second

    async def test_id_reaches_a_handler_log_line(
        self, api: httpx.AsyncClient, captured_logs
    ) -> None:
        """The point of the contextvar: a line emitted inside the ingest handler carries the
        same id as the access log line, without the id being threaded through five layers
        of signatures."""
        supplied = f"trace-{uuid4().hex[:8]}"
        await api.post(
            "/api/v1/events",
            json={"event_type": "a.b", "payload": {}},
            headers={"X-Request-ID": supplied},
        )

        ingested = [e for e in captured_logs if e.get("event") == "event ingested"]
        assert ingested
        assert ingested[-1]["request_id"] == supplied


class TestStructuredLogs:
    async def test_access_line_fields(self, api: httpx.AsyncClient, captured_logs) -> None:
        await api.get("/api/v1/endpoints")

        lines = [e for e in captured_logs if e.get("event") == "request"]
        assert lines
        line = lines[-1]
        for field in ("request_id", "method", "path", "route", "status", "duration_ms"):
            assert field in line, field
        assert isinstance(line["duration_ms"], int | float)
        assert line["status"] == 200

    async def test_ingest_line_carries_domain_fields(
        self, api: httpx.AsyncClient, captured_logs
    ) -> None:
        """Queryable fields, not a formatted sentence.

        "Every ingest with zero subscribers in the last hour" is a filter on
        deliveries_scheduled, not a regex over prose.
        """
        await api.post("/api/v1/events", json={"event_type": "a.b", "payload": {}})

        lines = [e for e in captured_logs if e.get("event") == "event ingested"]
        assert lines
        line = lines[-1]
        assert "event_id" in line
        assert line["event_type"] == "a.b"
        assert line["deliveries_scheduled"] == 0
        assert line["duplicate"] is False

    async def test_probes_are_not_logged(self, api: httpx.AsyncClient, captured_logs) -> None:
        """A probe hitting /health every second would drown everything else."""
        before = len([e for e in captured_logs if e.get("event") == "request"])
        for _ in range(5):
            await api.get("/health")
        after = len([e for e in captured_logs if e.get("event") == "request"])
        assert after == before

    def test_json_renderer_produces_one_object_per_line(self) -> None:
        """A log shipper needs a parseable line, which rules out multi-line output."""
        renderer = structlog.processors.JSONRenderer()
        rendered = renderer(None, "info", {"event": "x", "delivery_id": "abc", "status": 503})
        assert "\n" not in rendered
        assert json.loads(rendered) == {"event": "x", "delivery_id": "abc", "status": 503}


class TestMetrics:
    async def test_endpoint_is_open_and_well_formed(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/metrics")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
        assert "# HELP hookline_http_requests_total" in response.text
        assert "# TYPE hookline_http_requests_total counter" in response.text

    async def test_route_label_is_the_full_template(self, api: httpx.AsyncClient) -> None:
        """One series per route, not per resource id.

        Labelling by concrete path would create a series per event - millions of them -
        and take the monitoring system down instead of monitoring it. The prefix is
        included because the matched route only knows its path within its own router.
        """
        created = (
            await api.post("/api/v1/endpoints", json={"url": "https://a.example.com/h"})
        ).json()
        await api.get(f"/api/v1/endpoints/{created['id']}")

        body = (await api.get("/metrics")).text
        assert 'route="/api/v1/endpoints/{endpoint_id}"' in body
        assert created["id"] not in body

    async def test_unmatched_paths_collapse_to_one_label(self, api: httpx.AsyncClient) -> None:
        """Requests to paths that match no route share a single label.

        The invariant being protected is label cardinality. A resource id in a route label
        means one time series per resource - millions of them - which is how a monitoring
        system gets taken down by the thing it was installed to monitor.
        """
        for _ in range(3):
            await api.get(f"/api/v1/nope/{uuid4()}")

        body = (await api.get("/metrics")).text
        routes = set(re.findall(r'hookline_http_requests_total\{[^}]*route="([^"]+)"', body))

        assert "unmatched" in routes
        # No label may contain a concrete id. Real routes appear as templates
        # (`/api/v1/events/{event_id}`), arbitrary paths as `unmatched`, and nothing else.
        assert not [r for r in routes if re.search(r"[0-9a-f]{8}-[0-9a-f]{4}", r)]
        assert all(r == "unmatched" or "{" in r or r.count("/") <= 3 for r in routes), routes

    async def test_counters_move(self, api: httpx.AsyncClient) -> None:
        before = parse_exposition((await api.get("/metrics")).text)
        await api.post("/api/v1/events", json={"event_type": "a.b", "payload": {}})
        after = parse_exposition((await api.get("/metrics")).text)

        ingested = 'hookline_events_ingested_total{duplicate="false"}'
        requests = 'hookline_http_requests_total{method="POST",route="/api/v1/events",status="202"}'
        assert after.get(ingested, 0) - before.get(ingested, 0) == 1
        assert after.get(requests, 0) - before.get(requests, 0) == 1

    async def test_idempotent_replays_are_counted_separately(self, api: httpx.AsyncClient) -> None:
        headers = {"Idempotency-Key": f"k-{uuid4().hex[:8]}"}
        body = {"event_type": "a.b", "payload": {}}

        before = parse_exposition((await api.get("/metrics")).text)
        await api.post("/api/v1/events", json=body, headers=headers)
        await api.post("/api/v1/events", json=body, headers=headers)
        after = parse_exposition((await api.get("/metrics")).text)

        duplicate = 'hookline_events_ingested_total{duplicate="true"}'
        assert after.get(duplicate, 0) - before.get(duplicate, 0) == 1

    async def test_probe_and_scrape_traffic_is_excluded(self, api: httpx.AsyncClient) -> None:
        """Prometheus scrapes every 15 seconds for ever; counting that tells you nothing
        and dominates the totals."""
        before = parse_exposition((await api.get("/metrics")).text)
        for _ in range(5):
            await api.get("/health")
            await api.get("/ready")
            await api.get("/metrics")
        after = parse_exposition((await api.get("/metrics")).text)

        assert not [k for k in after if 'route="/health"' in k or 'route="/metrics"' in k]

        def total(sample: dict[str, float]) -> float:
            return sum(v for k, v in sample.items() if k.startswith("hookline_http_requests_total"))

        assert total(after) == total(before)

    async def test_queue_gauges_report_every_status_including_zero(
        self, api: httpx.AsyncClient
    ) -> None:
        """PromQL treats an absent series very differently from one holding 0, so an alert
        on `hookline_deliveries{status="dead"}` must not silently stop evaluating."""
        gauges = parse_exposition((await api.get("/metrics")).text)
        for status in ("pending", "in_flight", "delivered", "failed", "dead"):
            assert f'hookline_deliveries{{status="{status}"}}' in gauges

    async def test_queue_gauge_tracks_the_table(self, api: httpx.AsyncClient) -> None:
        event_type = f"gauge.{uuid4().hex[:6]}"
        await api.post(
            "/api/v1/endpoints",
            json={"url": "https://g.example.com/h", "event_types": [event_type]},
        )

        before = parse_exposition((await api.get("/metrics")).text)
        await api.post("/api/v1/events", json={"event_type": event_type, "payload": {}})
        after = parse_exposition((await api.get("/metrics")).text)

        key = 'hookline_deliveries{status="pending"}'
        assert after[key] - before.get(key, 0) == 1

    def test_status_class_collapses_codes(self) -> None:
        """5xx is one series. Nobody alerts on the difference between 502 and 503."""
        assert metrics.status_class(200) == "2xx"
        assert metrics.status_class(404) == "4xx"
        assert metrics.status_class(503) == "5xx"
        assert metrics.status_class(None) == "none"

    def test_metric_naming_convention(self) -> None:
        """Boring consistency is what makes dashboards transferable between services."""
        exported = metrics.REGISTRY.collect()
        for family in exported:
            if not family.name.startswith("hookline_"):
                continue
            if family.type == "counter":
                assert family.name.endswith("_total") or family.documentation
            assert re.fullmatch(r"hookline_[a-z0-9_]+", family.name), family.name
