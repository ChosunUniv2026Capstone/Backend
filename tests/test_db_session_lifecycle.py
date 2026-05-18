from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy.orm import Session

from app import db as db_module


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
