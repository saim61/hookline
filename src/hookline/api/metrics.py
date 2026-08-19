from fastapi import APIRouter, HTTPException, Response, status
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import func, select

from hookline.api.deps import SessionDep, SettingsDep
from hookline.enums import DeliveryStatus
from hookline.models.delivery import Delivery
from hookline.observability import metrics

router = APIRouter(tags=["meta"])


@router.get("/metrics", summary="Prometheus metrics", include_in_schema=False)
async def prometheus_metrics(session: SessionDep, settings: SettingsDep) -> Response:
    """Scrape endpoint.

    Unauthenticated, like the health probes: Prometheus does not carry an API key, and in
    a cluster this port is not routed publicly. If it must be exposed, put the auth at the
    ingress rather than here.

    Excluded from the OpenAPI schema - it is not part of the product's API surface and
    would just be noise in the docs.
    """
    if not settings.metrics_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="metrics disabled")

    await _sample_queue_depth(session)
    return Response(generate_latest(metrics.REGISTRY), media_type=CONTENT_TYPE_LATEST)


async def _sample_queue_depth(session: SessionDep) -> None:
    """Read the queue gauges at scrape time.

    These describe the table, not this process, so they cannot be maintained by
    incrementing counters in application code - a delivery created by replica A has to
    show up in replica B's numbers too. Sampling on scrape keeps them honest at the cost
    of two aggregate queries every scrape interval.
    """
    rows = await session.execute(select(Delivery.status, func.count()).group_by(Delivery.status))
    counts = {status_value: count for status_value, count in rows}
    # Every known status is reported, including zeroes. A gauge that simply disappears
    # when the count is zero makes `hookline_deliveries{status="dead"}` go from 3 to
    # absent, and PromQL treats absent very differently from 0 in alert expressions.
    for member in DeliveryStatus:
        metrics.deliveries_by_status.labels(status=member.value).set(counts.get(member, 0))

    oldest = await session.execute(
        select(func.min(Delivery.next_attempt_at)).where(
            Delivery.status == DeliveryStatus.PENDING,
            Delivery.next_attempt_at <= func.now(),
        )
    )
    due_since = oldest.scalar_one_or_none()
    if due_since is None:
        # Nothing overdue. Zero is the truthful reading of "the queue is keeping up".
        metrics.oldest_pending_age.set(0)
        return

    # The database's clock, not this process's. The timestamps being compared were written
    # by Postgres, so measuring the gap against a possibly-skewed app clock would report
    # a lag that does not exist - or hide one that does.
    now = await session.scalar(select(func.now()))
    if now is None:  # pragma: no cover - select now() cannot return NULL
        return
    metrics.oldest_pending_age.set(max(0.0, (now - due_since).total_seconds()))
