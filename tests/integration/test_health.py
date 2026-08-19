import httpx


class TestLiveness:
    async def test_health_is_ok(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    async def test_health_does_not_touch_the_database(self, client: httpx.AsyncClient) -> None:
        """Liveness must not depend on Postgres.

        Kubernetes restarts the pod when liveness fails, so a brief database blip would
        restart every replica at once - turning a recoverable outage into a much worse one.
        Readiness is the probe that is allowed to care.
        """
        import hookline.api.health as health_module

        source = health_module.health.__code__.co_names
        assert "execute" not in source
        assert "session" not in health_module.health.__code__.co_varnames


class TestReadiness:
    async def test_reports_both_dependencies(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/ready")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["database"] == "ok"
        assert body["redis"] == "ok"

    async def test_redis_being_down_is_reported_but_not_fatal(
        self, client: httpx.AsyncClient, monkeypatch
    ) -> None:
        """Failing readiness on Redis would pull every pod from the load balancer to
        protect a feature that is already designed to be optional - every Redis caller
        fails open, so ingest keeps working without it."""
        from hookline.api import health as health_module

        async def unreachable() -> bool:
            return False

        monkeypatch.setattr(health_module.cache_client, "ping", unreachable)

        response = await client.get("/ready")
        assert response.status_code == 200
        assert response.json()["redis"] == "degraded"
        assert response.json()["database"] == "ok"

    async def test_database_being_down_is_503(self, client: httpx.AsyncClient, monkeypatch) -> None:
        """Postgres is the source of truth: without it nothing can be ingested, so the pod
        should leave the load balancer."""
        from hookline.db import session as session_module

        class Broken:
            async def execute(self, *args: object, **kwargs: object) -> None:
                raise RuntimeError("connection refused")

            async def commit(self) -> None:
                pass

            async def rollback(self) -> None:
                pass

        async def broken_session():
            yield Broken()

        from hookline.main import create_app

        app = create_app()
        app.dependency_overrides[session_module.get_session] = broken_session
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            response = await c.get("/ready")

        assert response.status_code == 503
        assert "database" in response.json()["detail"]
