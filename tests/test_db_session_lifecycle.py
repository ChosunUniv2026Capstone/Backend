from __future__ import annotations

from collections.abc import Iterator
import inspect

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import db as db_module
from app.db import get_db
from app.main import app


class TrackingSession:
    def __init__(self) -> None:
        self.rollback_called = False
        self.close_called = False

    def in_transaction(self) -> bool:
        return True

    def rollback(self) -> None:
        self.rollback_called = True

    def close(self) -> None:
        self.close_called = True


def test_get_db_rolls_back_open_transaction_before_close(monkeypatch) -> None:
    session = TrackingSession()

    def session_factory() -> TrackingSession:
        return session

    monkeypatch.setattr(db_module, "SessionLocal", session_factory)

    dependency: Iterator[Session] = db_module.get_db()
    assert next(dependency) is session

    try:
        next(dependency)
    except StopIteration:
        pass

    assert session.rollback_called is True
    assert session.close_called is True


def test_engine_uses_configured_pool_guardrails() -> None:
    pool = db_module.engine.pool

    assert pool.size() == db_module.settings.db_pool_size
    assert pool._max_overflow == db_module.settings.db_max_overflow
    assert pool._timeout == db_module.settings.db_pool_timeout
    assert pool._recycle == db_module.settings.db_pool_recycle
    assert pool._pre_ping == db_module.settings.db_pool_pre_ping


def test_health_is_async_and_does_not_touch_db() -> None:
    health_route = next(route for route in app.routes if getattr(route, "path", None) == "/health")
    assert inspect.iscoroutinefunction(health_route.endpoint)
    assert not health_route.dependant.dependencies

    def fail_get_db() -> Iterator[Session]:
        raise AssertionError("/health must not request a DB session")
        yield  # pragma: no cover

    app.dependency_overrides[get_db] = fail_get_db
    try:
        response = TestClient(app).get("/health")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
