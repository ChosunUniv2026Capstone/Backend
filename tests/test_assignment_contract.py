from __future__ import annotations

from datetime import UTC, datetime, timedelta
from io import BytesIO

import pytest
from fastapi import HTTPException, UploadFile
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import app.assignments as assignments_module
from app.assignments import create_professor_assignment, submit_student_assignment
from app.models import Assignment, Base, Course, CourseEnrollment, User


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


def seed_assignment_context(session: Session) -> dict[str, int]:
    student = User(student_id="20201239", name="Kim Student 06", role="student", password="devpass123")
    professor = User(professor_id="PRF002", name="Lee Professor 02", role="professor", password="devpass123")
    session.add_all([student, professor])
    session.flush()

    course = Course(course_code="CSE116", title="Capstone Design A", professor_user_id=professor.id)
    session.add(course)
    session.flush()
    session.add(CourseEnrollment(course_id=course.id, student_user_id=student.id, status="active"))

    now = datetime.now(UTC)
    assignment = Assignment(
        course_id=course.id,
        title="Open Assignment",
        opens_at=now - timedelta(hours=1),
        due_at=now + timedelta(hours=1),
    )
    session.add(assignment)
    session.commit()

    return {"student_user_id": student.id, "course_id": course.id, "assignment_id": assignment.id}


def test_create_assignment_rejects_title_over_db_limit(db_session: Session) -> None:
    ctx = seed_assignment_context(db_session)

    with pytest.raises(HTTPException) as exc_info:
        create_professor_assignment(
            db_session,
            course_id=ctx["course_id"],
            payload={
                "title": "x" * 201,
                "description": None,
                "opens_at": datetime.now(UTC),
                "due_at": datetime.now(UTC) + timedelta(days=1),
            },
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["code"] == "ASSIGNMENT_INVALID_PAYLOAD"
    assert exc_info.value.detail["details"]["field"] == "title"
    assert exc_info.value.detail["details"]["max_length"] == 200


def test_submission_truncates_original_filename_to_db_limit(
    db_session: Session,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = seed_assignment_context(db_session)
    monkeypatch.setattr(assignments_module.settings, "assignment_upload_dir", str(tmp_path))
    monkeypatch.setattr(assignments_module, "_assignment_status", lambda assignment, now=None: "open")

    upload = UploadFile(file=BytesIO(b"hello"), filename=("report-" + "x" * 300 + ".txt"))
    detail = submit_student_assignment(
        db_session,
        student_user_id=ctx["student_user_id"],
        course_id=ctx["course_id"],
        assignment_id=ctx["assignment_id"],
        submission_text="done",
        files=[upload],
    )

    [attachment] = detail["submission"]["attachments"]
    assert len(attachment["original_filename"]) == 255
    assert attachment["file_size_bytes"] == 5
