from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import services as services_module
from app.db import get_db
from app.main import app
from app.models import Base, Classroom, Course, CourseEnrollment, CourseSchedule, Exam, ExamQuestion, Notice, User
from app.storage import get_storage_backend


def auth_header(login_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer dev-token:{login_id}"}


def make_client(tmp_path: Path) -> TestClient:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    Base.metadata.create_all(engine)

    with SessionLocal.begin() as session:
        student = User(student_id="20201239", name="Kim Student", role="student", password="devpass123")
        other_student = User(student_id="20209999", name="Other Student", role="student", password="devpass123")
        professor = User(professor_id="PRF002", name="Lee Professor", role="professor", password="devpass123")
        other_professor = User(professor_id="PRF003", name="Other Professor", role="professor", password="devpass123")
        session.add_all([student, other_student, professor, other_professor])
        session.flush()
        course = Course(course_code="CSE116", title="Capstone", professor_user_id=professor.id)
        other_course = Course(course_code="CSE999", title="Other", professor_user_id=other_professor.id)
        classroom = Classroom(classroom_code="B101", name="Lab")
        session.add_all([course, other_course, classroom])
        session.flush()
        session.add_all([
            CourseEnrollment(course_id=course.id, student_user_id=student.id, status="active"),
            CourseEnrollment(course_id=other_course.id, student_user_id=other_student.id, status="active"),
            CourseSchedule(course_id=course.id, classroom_id=classroom.id, day_of_week=0, starts_at=time(9, 0), ends_at=time(10, 0)),
        ])
        exam = Exam(
            course_id=course.id,
            title="Media Exam",
            exam_type="quiz",
            status="published",
            starts_at=datetime.now(UTC) - timedelta(minutes=5),
            ends_at=datetime.now(UTC) + timedelta(hours=1),
            duration_minutes=30,
        )
        session.add(exam)
        session.flush()
        session.add(ExamQuestion(exam_id=exam.id, question_order=1, question_type="multiple_choice", prompt="Q", points=1))
        session.add(Notice(course_id=course.id, author_user_id=professor.id, title="Seed", body="Body"))

    def override_get_db() -> Generator[Session, None, None]:
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    services_module.settings.object_storage_provider = "local"
    services_module.settings.object_storage_local_dir = str(tmp_path)
    get_storage_backend.cache_clear()
    return TestClient(app)


def test_learning_item_upload_download_and_cross_role_denial(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    response = client.post(
        "/api/professors/PRF002/courses/CSE116/learning-items",
        headers=auth_header("PRF002"),
        data={"kind": "material", "title": "Week 1", "description": "Intro"},
        files=[("files", ("intro.txt", b"hello learning", "text/plain"))],
    )
    assert response.status_code == 201, response.text
    item = response.json()
    assert item["attachments"][0]["original_filename"] == "intro.txt"

    listing = client.get("/api/students/20201239/courses/CSE116/learning-items", headers=auth_header("20201239"))
    assert listing.status_code == 200
    assert listing.json()[0]["title"] == "Week 1"

    attachment_id = item["attachments"][0]["id"]
    download = client.get(
        f"/api/students/20201239/courses/CSE116/learning-items/{item['id']}/attachments/{attachment_id}",
        headers=auth_header("20201239"),
    )
    assert download.status_code == 200
    assert download.content == b"hello learning"

    denied = client.get(
        f"/api/students/20209999/courses/CSE116/learning-items/{item['id']}/attachments/{attachment_id}",
        headers=auth_header("20209999"),
    )
    assert denied.status_code == 403


def test_notice_exam_media_and_report_exports(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    notice = client.post(
        "/api/professors/PRF002/notices",
        headers=auth_header("PRF002"),
        data={"title": "Attached", "body": "See file", "course_code": "CSE116"},
        files=[("files", ("notice.txt", b"notice-body", "text/plain"))],
    )
    assert notice.status_code == 201, notice.text
    notice_payload = notice.json()["data"]
    notice_attachment_id = notice_payload["attachments"][0]["id"]
    notice_download = client.get(
        f"/api/notices/20201239/{notice_payload['id']}/attachments/{notice_attachment_id}",
        headers=auth_header("20201239"),
    )
    assert notice_download.status_code == 200
    assert notice_download.content == b"notice-body"

    exam_detail = client.get("/api/professors/PRF002/courses/CSE116/exams/1", headers=auth_header("PRF002"))
    question_id = exam_detail.json()["questions"][0]["id"]
    media = client.post(
        f"/api/professors/PRF002/courses/CSE116/exams/1/questions/{question_id}/attachments",
        headers=auth_header("PRF002"),
        files=[("files", ("diagram.png", b"png-bytes", "image/png"))],
    )
    assert media.status_code == 201, media.text
    media_id = media.json()[0]["id"]
    media_download = client.get(
        f"/api/students/20201239/courses/CSE116/exams/1/questions/{question_id}/attachments/{media_id}",
        headers=auth_header("20201239"),
    )
    assert media_download.status_code == 200
    assert media_download.content == b"png-bytes"

    export = client.post(
        "/api/professors/PRF002/courses/CSE116/attendance/report-exports",
        headers=auth_header("PRF002"),
        json={"export_type": "attendance_csv"},
    )
    assert export.status_code == 201, export.text
    export_id = export.json()["id"]
    exports = client.get("/api/professors/PRF002/courses/CSE116/attendance/report-exports", headers=auth_header("PRF002"))
    assert exports.status_code == 200
    assert exports.json()[0]["id"] == export_id
    download = client.get(
        f"/api/professors/PRF002/courses/CSE116/attendance/report-exports/{export_id}/download",
        headers=auth_header("PRF002"),
    )
    assert download.status_code == 200
    assert b"course_code,generated_at" in download.content
