from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import Session, sessionmaker

import app.services as services_module
from app.models import Base, Course, CourseEnrollment, Exam, RegisteredDevice, User
from app.schemas import ProfessorExamCreateRequest
from app.services import (
    get_student_exam_detail,
    list_student_exams,
    save_student_exam_answer,
    start_student_exam,
    submit_student_exam,
)


class DummyPresenceClient:
    pass


@pytest.fixture
def db_session() -> Session:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    Base.metadata.create_all(engine)

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def seed_exam_context(session: Session) -> dict[str, int]:
    student = User(student_id="20201239", name="Kim Student 06", role="student", password="devpass123")
    professor = User(professor_id="PRF002", name="Lee Professor 02", role="professor", password="devpass123")
    session.add_all([student, professor])
    session.flush()

    course = Course(course_code="CSE116", title="Capstone Design A", professor_user_id=professor.id)
    session.add(course)
    session.flush()

    session.add(CourseEnrollment(course_id=course.id, student_user_id=student.id, status="active"))
    session.add(
        RegisteredDevice(
            user_id=student.id,
            label="Kim Phone",
            mac_address="52:54:00:12:34:56",
            status="active",
        )
    )

    now = datetime.now(UTC)
    session.add_all(
        [
            Exam(
                course_id=course.id,
                title="Archived Exam",
                exam_type="quiz",
                status="archived",
                starts_at=now - timedelta(days=2),
                ends_at=now - timedelta(days=1),
                duration_minutes=30,
            ),
            Exam(
                course_id=course.id,
                title="Published Exam",
                exam_type="quiz",
                status="published",
                starts_at=now + timedelta(hours=1),
                ends_at=now + timedelta(hours=2),
                duration_minutes=30,
            ),
            Exam(
                course_id=course.id,
                title="Closed Exam",
                exam_type="quiz",
                status="closed",
                starts_at=now - timedelta(days=1),
                ends_at=now - timedelta(hours=12),
                duration_minutes=30,
            ),
        ]
    )
    session.commit()

    archived_exam = session.query(Exam).filter_by(title="Archived Exam").one()
    published_exam = session.query(Exam).filter_by(title="Published Exam").one()
    closed_exam = session.query(Exam).filter_by(title="Closed Exam").one()

    return {
        "student_user_id": student.id,
        "course_id": course.id,
        "archived_exam_id": archived_exam.id,
        "published_exam_id": published_exam.id,
        "closed_exam_id": closed_exam.id,
    }


def test_student_list_excludes_archived_exams(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = seed_exam_context(db_session)

    class _NaiveDateTime:
        @staticmethod
        def now(_tz=None) -> datetime:
            return datetime.now()

    monkeypatch.setattr(services_module, "datetime", _NaiveDateTime)
    exams = list_student_exams(db_session, ctx["student_user_id"], ctx["course_id"])

    assert [exam["title"] for exam in exams] == ["Closed Exam", "Published Exam"]


def test_archived_exam_is_hidden_from_student_detail_and_actions(db_session: Session) -> None:
    ctx = seed_exam_context(db_session)
    archived_exam_id = ctx["archived_exam_id"]

    with pytest.raises(HTTPException) as detail_exc:
        get_student_exam_detail(db_session, ctx["student_user_id"], ctx["course_id"], archived_exam_id)
    assert detail_exc.value.status_code == 404
    assert detail_exc.value.detail["code"] == "EXAM_NOT_FOUND"

    with pytest.raises(HTTPException) as start_exc:
        start_student_exam(
            db=db_session,
            presence_client=DummyPresenceClient(),
            student_id="20201239",
            student_user_id=ctx["student_user_id"],
            course_code="CSE116",
            course_id=ctx["course_id"],
            exam_id=archived_exam_id,
        )
    assert start_exc.value.status_code == 404
    assert start_exc.value.detail["code"] == "EXAM_NOT_FOUND"

    with pytest.raises(HTTPException) as submit_exc:
        submit_student_exam(
            db=db_session,
            student_user_id=ctx["student_user_id"],
            course_id=ctx["course_id"],
            exam_id=archived_exam_id,
            payload={"answers": []},
        )
    assert submit_exc.value.status_code == 404
    assert submit_exc.value.detail["code"] == "EXAM_NOT_FOUND"

    with pytest.raises(HTTPException) as save_exc:
        save_student_exam_answer(
            db=db_session,
            student_user_id=ctx["student_user_id"],
            course_id=ctx["course_id"],
            exam_id=archived_exam_id,
            submission_id=1,
            question_id=1,
            payload={"selected_option_id": 1},
        )
    assert save_exc.value.status_code == 404
    assert save_exc.value.detail["code"] == "EXAM_NOT_FOUND"


def test_professor_exam_request_defaults_requires_presence_true() -> None:
    payload = ProfessorExamCreateRequest(
        title="Exam",
        starts_at=datetime.now(UTC),
        ends_at=datetime.now(UTC) + timedelta(hours=1),
        duration_minutes=30,
        questions=[],
    )

    assert payload.requires_presence is True


def test_exam_model_defaults_requires_presence_true(db_session: Session) -> None:
    student = User(student_id="20201239", name="Kim Student 06", role="student", password="devpass123")
    professor = User(professor_id="PRF002", name="Lee Professor 02", role="professor", password="devpass123")
    db_session.add_all([student, professor])
    db_session.flush()

    course = Course(course_code="CSE116", title="Capstone Design A", professor_user_id=professor.id)
    db_session.add(course)
    db_session.flush()

    exam = Exam(
        course_id=course.id,
        title="Default Presence Exam",
        exam_type="quiz",
        status="draft",
        starts_at=datetime.now(UTC),
        ends_at=datetime.now(UTC) + timedelta(hours=1),
        duration_minutes=30,
    )
    db_session.add(exam)
    db_session.flush()

    assert exam.requires_presence is True
