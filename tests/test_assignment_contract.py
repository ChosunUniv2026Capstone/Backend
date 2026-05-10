from __future__ import annotations

from datetime import UTC, datetime, timedelta
from io import BytesIO

import pytest
from fastapi import HTTPException, UploadFile
from sqlalchemy import create_engine
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import app.assignments as assignments_module
from app.assignments import create_professor_assignment, submit_student_assignment
from app.models import Assignment, AssignmentSubmissionAttachment, Base, Course, CourseEnrollment, User
from app.storage import get_storage_backend


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
    monkeypatch.setattr(assignments_module.settings, "object_storage_provider", "local")
    monkeypatch.setattr(assignments_module.settings, "object_storage_local_dir", str(tmp_path))
    get_storage_backend.cache_clear()
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


def test_assignment_submission_replaces_old_objects_after_commit(
    db_session: Session,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = seed_assignment_context(db_session)
    monkeypatch.setattr(assignments_module.settings, "object_storage_provider", "local")
    monkeypatch.setattr(assignments_module.settings, "object_storage_local_dir", str(tmp_path))
    monkeypatch.setattr(assignments_module, "_assignment_status", lambda assignment, now=None: "open")
    get_storage_backend.cache_clear()

    first = UploadFile(file=BytesIO(b"first"), filename="first.txt")
    submit_student_assignment(
        db_session,
        student_user_id=ctx["student_user_id"],
        course_id=ctx["course_id"],
        assignment_id=ctx["assignment_id"],
        submission_text="first",
        files=[first],
    )
    old_attachment = db_session.scalar(select(AssignmentSubmissionAttachment))
    assert old_attachment is not None
    old_path = tmp_path / old_attachment.storage_key
    assert old_path.exists()

    second = UploadFile(file=BytesIO(b"second"), filename="second.txt")
    detail = submit_student_assignment(
        db_session,
        student_user_id=ctx["student_user_id"],
        course_id=ctx["course_id"],
        assignment_id=ctx["assignment_id"],
        submission_text="second",
        files=[second],
    )

    assert not old_path.exists()
    [attachment] = detail["submission"]["attachments"]
    current_attachment = db_session.get(AssignmentSubmissionAttachment, attachment["id"])
    assert current_attachment is not None
    assert (tmp_path / current_attachment.storage_key).read_bytes() == b"second"


def test_assignment_replacement_uses_deletion_outbox_when_available(
    db_session: Session,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = seed_assignment_context(db_session)
    monkeypatch.setattr(assignments_module.settings, "object_storage_provider", "local")
    monkeypatch.setattr(assignments_module.settings, "object_storage_local_dir", str(tmp_path))
    monkeypatch.setattr(assignments_module, "_assignment_status", lambda assignment, now=None: "open")
    get_storage_backend.cache_clear()
    db_session.execute(text("""
        CREATE TABLE object_deletion_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            storage_provider TEXT NOT NULL,
            bucket_name TEXT NULL,
            storage_key TEXT NOT NULL,
            owner_domain TEXT NOT NULL,
            owner_id INTEGER NULL,
            reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP NULL
        )
    """))
    db_session.commit()

    submit_student_assignment(
        db_session,
        student_user_id=ctx["student_user_id"],
        course_id=ctx["course_id"],
        assignment_id=ctx["assignment_id"],
        submission_text="first",
        files=[UploadFile(file=BytesIO(b"first"), filename="first.txt")],
    )
    old_attachment = db_session.scalar(select(AssignmentSubmissionAttachment))
    assert old_attachment is not None
    old_path = tmp_path / old_attachment.storage_key

    submit_student_assignment(
        db_session,
        student_user_id=ctx["student_user_id"],
        course_id=ctx["course_id"],
        assignment_id=ctx["assignment_id"],
        submission_text="second",
        files=[UploadFile(file=BytesIO(b"second"), filename="second.txt")],
    )

    queued = db_session.execute(
        text("SELECT storage_provider, bucket_name, storage_key, status FROM object_deletion_jobs")
    ).mappings().one()
    assert queued["storage_provider"] == "local"
    assert queued["bucket_name"] == "local"
    assert queued["storage_key"] == old_attachment.storage_key
    assert queued["status"] == "completed"
    assert not old_path.exists()

    result = assignments_module.process_object_deletion_jobs(db_session)

    assert result == {"processed": 0, "deleted": 0, "failed": 0, "skipped": False}


def test_deletion_worker_uses_job_provider_and_bucket(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    deleted: list[tuple[str | None, str | None, str]] = []

    class RecordingBackend:
        def __init__(self, provider: str | None, bucket_name: str | None) -> None:
            self.provider = provider or "local"
            self.bucket_name = bucket_name

        def delete_object(self, key: str) -> None:
            deleted.append((self.provider, self.bucket_name, key))

    db_session.execute(text("""
        CREATE TABLE object_deletion_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            storage_provider TEXT NOT NULL,
            bucket_name TEXT NOT NULL,
            storage_key TEXT NOT NULL,
            owner_domain TEXT NOT NULL,
            owner_id INTEGER NULL,
            reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP NULL
        )
    """))
    db_session.execute(text("""
        INSERT INTO object_deletion_jobs
            (storage_provider, bucket_name, storage_key, owner_domain, reason)
        VALUES
            ('s3', 'smart-class-alt', 'assignments/provider-aware.txt', 'assignment_submission_attachment', 'test')
    """))
    db_session.commit()
    monkeypatch.setattr(
        assignments_module,
        "get_storage_backend_for_metadata",
        lambda provider, bucket_name: RecordingBackend(provider, bucket_name),
    )

    result = assignments_module.process_object_deletion_jobs(db_session)

    assert result == {"processed": 1, "deleted": 1, "failed": 0, "skipped": False}
    assert deleted == [("s3", "smart-class-alt", "assignments/provider-aware.txt")]
    assert db_session.execute(text("SELECT status FROM object_deletion_jobs")).scalar_one() == "completed"
