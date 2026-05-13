from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import Session, sessionmaker

import app.services as services_module
from app.models import Base, Course, CourseEnrollment, Exam, ExamQuestion, ExamQuestionOption, ExamSubmission, RegisteredDevice, User
from app.schemas import ProfessorExamCreateRequest
from app.services import (
    get_student_exam_detail,
    list_student_exams,
    create_professor_exam,
    save_student_exam_answer,
    start_student_exam,
    submit_student_exam,
    update_professor_exam,
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




def professor_exam_payload(**overrides) -> dict:
    now = datetime.now(UTC)
    payload = {
        "title": "Presence Optional Quiz",
        "description": None,
        "exam_type": "quiz",
        "starts_at": now + timedelta(minutes=5),
        "ends_at": now + timedelta(hours=1),
        "duration_minutes": 30,
        "requires_presence": False,
        "late_entry_allowed": True,
        "auto_submit_enabled": True,
        "shuffle_questions": False,
        "shuffle_options": False,
        "max_attempts": 1,
        "questions": [
            {
                "question_type": "multiple_choice",
                "prompt": "Pick one",
                "points": 1,
                "explanation": None,
                "is_required": True,
                "options": [
                    {"option_text": "A", "is_correct": True},
                    {"option_text": "B", "is_correct": False},
                ],
            }
        ],
    }
    payload.update(overrides)
    return payload

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


def test_professor_exam_create_and_update_preserve_requires_presence_false(db_session: Session) -> None:
    ctx = seed_exam_context(db_session)
    created = create_professor_exam(db=db_session, course_id=ctx["course_id"], payload=professor_exam_payload())

    assert created["requires_presence"] is False

    updated = update_professor_exam(
        db=db_session,
        course_id=ctx["course_id"],
        exam_id=created["id"],
        payload=professor_exam_payload(title="Still Optional"),
    )

    assert updated["requires_presence"] is False


def test_non_presence_exam_start_skips_presence_client(db_session: Session) -> None:
    ctx = seed_exam_context(db_session)
    now = datetime.now(UTC)
    exam = Exam(
        course_id=ctx["course_id"],
        title="No Presence Exam",
        exam_type="quiz",
        status="published",
        starts_at=now - timedelta(minutes=5),
        ends_at=now + timedelta(hours=1),
        duration_minutes=30,
        requires_presence=False,
    )
    db_session.add(exam)
    db_session.commit()

    class FailingPresenceClient:
        def check_eligibility(self, **_kwargs):
            raise AssertionError("presence client should not be called")

    started = start_student_exam(
        db=db_session,
        presence_client=FailingPresenceClient(),
        student_id="20201239",
        student_user_id=ctx["student_user_id"],
        course_code="CSE116",
        course_id=ctx["course_id"],
        exam_id=exam.id,
    )

    assert started["status"] == "in_progress"


def test_save_student_exam_answer_rejects_expired_submission(db_session: Session) -> None:
    ctx = seed_exam_context(db_session)
    now = datetime.now(UTC)
    exam = Exam(
        course_id=ctx["course_id"],
        title="Expired Answer Save",
        exam_type="quiz",
        status="published",
        starts_at=now - timedelta(hours=1),
        ends_at=now + timedelta(hours=1),
        duration_minutes=30,
    )
    db_session.add(exam)
    db_session.flush()
    question = ExamQuestion(exam_id=exam.id, question_order=1, question_type="multiple_choice", prompt="Pick one", points=1)
    db_session.add(question)
    db_session.flush()
    option = ExamQuestionOption(question_id=question.id, option_order=1, option_text="A", is_correct=True)
    db_session.add(option)
    submission = ExamSubmission(
        exam_id=exam.id,
        student_user_id=ctx["student_user_id"],
        attempt_no=1,
        status="in_progress",
        started_at=now - timedelta(hours=1),
        expires_at=now - timedelta(minutes=1),
        time_limit_snapshot_minutes=30,
    )
    db_session.add(submission)
    db_session.commit()

    with pytest.raises(HTTPException) as exc_info:
        save_student_exam_answer(
            db=db_session,
            student_user_id=ctx["student_user_id"],
            course_id=ctx["course_id"],
            exam_id=exam.id,
            submission_id=submission.id,
            question_id=question.id,
            payload={"selected_option_id": option.id},
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "EXAM_SUBMISSION_EXPIRED"
