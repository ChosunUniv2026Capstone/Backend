from __future__ import annotations
from envelope import api_json

import csv
from collections.abc import Generator
from datetime import UTC, datetime, time, timedelta
from io import StringIO
from pathlib import Path

import pytest
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


def make_client(tmp_path: Path, *, raise_server_exceptions: bool = True) -> TestClient:
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
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def _csv_rows(response) -> list[list[str]]:
    return list(csv.reader(StringIO(response.content.decode("utf-8-sig"))))


def _seed_two_slot_attendance(client: TestClient) -> None:
    timeline = client.get("/api/professors/PRF002/courses/CSE116/attendance/timeline", headers=auth_header("PRF002"))
    assert timeline.status_code == 200, timeline.text
    projection_keys = [slot["projection_key"] for slot in api_json(timeline)["weeks"][0]["slots"][:2]]
    open_response = client.post(
        "/api/professors/PRF002/courses/CSE116/attendance/sessions/batch",
        headers=auth_header("PRF002"),
        json={"projection_keys": projection_keys, "mode": "manual"},
    )
    assert open_response.status_code == 200, open_response.text
    session_id = api_json(open_response)["changed_session_ids"][0]
    present = client.patch(
        f"/api/professors/PRF002/attendance/sessions/{session_id}/students/20201239",
        headers=auth_header("PRF002"),
        json={"status": "present", "projection_key": projection_keys[0]},
    )
    assert present.status_code == 200, present.text
    sick = client.patch(
        f"/api/professors/PRF002/attendance/sessions/{session_id}/students/20201239",
        headers=auth_header("PRF002"),
        json={"status": "sick", "projection_key": projection_keys[1]},
    )
    assert sick.status_code == 200, sick.text


def test_learning_item_upload_download_and_cross_role_denial(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    response = client.post(
        "/api/professors/PRF002/courses/CSE116/learning-items",
        headers=auth_header("PRF002"),
        data={"kind": "material", "title": "Week 1", "description": "Intro"},
        files=[("files", ("intro.txt", b"hello learning", "text/plain"))],
    )
    assert response.status_code == 201, response.text
    item = api_json(response)
    assert item["attachments"][0]["original_filename"] == "intro.txt"

    listing = client.get("/api/students/20201239/courses/CSE116/learning-items", headers=auth_header("20201239"))
    assert listing.status_code == 200
    assert api_json(listing)[0]["title"] == "Week 1"

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


def test_json_learning_item_download_is_not_wrapped_by_api_envelope(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    response = client.post(
        "/api/professors/PRF002/courses/CSE116/learning-items",
        headers=auth_header("PRF002"),
        data={"kind": "material", "title": "JSON Data", "description": "Raw JSON file"},
        files=[("files", ("data.json", b'{"x":1}', "application/json"))],
    )
    assert response.status_code == 201, response.text
    item = api_json(response)
    attachment_id = item["attachments"][0]["id"]

    download = client.get(
        f"/api/students/20201239/courses/CSE116/learning-items/{item['id']}/attachments/{attachment_id}",
        headers=auth_header("20201239"),
    )

    assert download.status_code == 200
    assert download.headers["content-disposition"].startswith("attachment;")
    assert download.content == b'{"x":1}'


def test_missing_learning_object_download_returns_404(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    response = client.post(
        "/api/professors/PRF002/courses/CSE116/learning-items",
        headers=auth_header("PRF002"),
        data={"kind": "material", "title": "Week 1", "description": "Intro"},
        files=[("files", ("intro.txt", b"hello learning", "text/plain"))],
    )
    assert response.status_code == 201, response.text
    item = api_json(response)
    attachment_id = item["attachments"][0]["id"]
    [stored_file] = [path for path in tmp_path.rglob("*") if path.is_file()]
    stored_file.unlink()

    download = client.get(
        f"/api/students/20201239/courses/CSE116/learning-items/{item['id']}/attachments/{attachment_id}",
        headers=auth_header("20201239"),
    )

    assert download.status_code == 404
    assert api_json(download)["detail"]["code"] == "OBJECT_NOT_FOUND"


def test_notice_attachment_upload_failure_rolls_back_notice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_client(tmp_path, raise_server_exceptions=False)

    def fail_upload(*args, **kwargs):
        raise RuntimeError("simulated object write failure")

    monkeypatch.setattr(services_module, "_store_domain_upload", fail_upload)
    response = client.post(
        "/api/professors/PRF002/notices",
        headers=auth_header("PRF002"),
        data={"title": "Attached", "body": "See file", "course_code": "CSE116"},
        files=[("files", ("notice.txt", b"notice-body", "text/plain"))],
    )
    assert response.status_code == 500

    notices = client.get("/api/notices/PRF002", headers=auth_header("PRF002"))
    assert notices.status_code == 200
    assert [notice["title"] for notice in api_json(notices)["data"]] == ["Seed"]


def test_learning_item_delete_processes_deletion_outbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_client(tmp_path)
    calls = []
    monkeypatch.setattr(services_module, "_process_object_deletion_jobs_if_available", lambda db: calls.append("processed") or {"processed": 0})
    response = client.post(
        "/api/professors/PRF002/courses/CSE116/learning-items",
        headers=auth_header("PRF002"),
        data={"kind": "material", "title": "Week 1", "description": "Intro"},
        files=[("files", ("intro.txt", b"hello learning", "text/plain"))],
    )
    assert response.status_code == 201, response.text
    item = api_json(response)

    delete_response = client.delete(
        f"/api/professors/PRF002/courses/CSE116/learning-items/{item['id']}",
        headers=auth_header("PRF002"),
    )

    assert delete_response.status_code == 204
    assert calls == ["processed"]


def test_notice_exam_media_and_report_exports(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    notice = client.post(
        "/api/professors/PRF002/notices",
        headers=auth_header("PRF002"),
        data={"title": "Attached", "body": "See file", "course_code": "CSE116"},
        files=[("files", ("notice.txt", b"notice-body", "text/plain"))],
    )
    assert notice.status_code == 201, notice.text
    notice_payload = api_json(notice)["data"]
    notice_attachment_id = notice_payload["attachments"][0]["id"]
    notice_download = client.get(
        f"/api/notices/20201239/{notice_payload['id']}/attachments/{notice_attachment_id}",
        headers=auth_header("20201239"),
    )
    assert notice_download.status_code == 200
    assert notice_download.content == b"notice-body"

    exam_detail = client.get("/api/professors/PRF002/courses/CSE116/exams/1", headers=auth_header("PRF002"))
    question_id = api_json(exam_detail)["questions"][0]["id"]
    media = client.post(
        f"/api/professors/PRF002/courses/CSE116/exams/1/questions/{question_id}/attachments",
        headers=auth_header("PRF002"),
        files=[("files", ("diagram.png", b"png-bytes", "image/png"))],
    )
    assert media.status_code == 201, media.text
    media_id = api_json(media)[0]["id"]
    media_download = client.get(
        f"/api/students/20201239/courses/CSE116/exams/1/questions/{question_id}/attachments/{media_id}",
        headers=auth_header("20201239"),
    )
    assert media_download.status_code == 200
    assert media_download.content == b"png-bytes"

    _seed_two_slot_attendance(client)

    export = client.post(
        "/api/professors/PRF002/courses/CSE116/attendance/report-exports",
        headers=auth_header("PRF002"),
        json={"export_type": "attendance_csv"},
    )
    assert export.status_code == 201, export.text
    export_id = api_json(export)["id"]
    exports = client.get("/api/professors/PRF002/courses/CSE116/attendance/report-exports", headers=auth_header("PRF002"))
    assert exports.status_code == 200
    assert api_json(exports)[0]["id"] == export_id
    download = client.get(
        f"/api/professors/PRF002/courses/CSE116/attendance/report-exports/{export_id}/download",
        headers=auth_header("PRF002"),
    )
    assert download.status_code == 200
    assert download.content.startswith(b"\xef\xbb\xbf")
    rows = _csv_rows(download)
    assert rows[0] == ["학번", "이름", "출석 차시", "결석 차시", "지각 차시", "공결 차시"]
    assert rows[1] == ["20201239", "Kim Student", "1", "0", "0", "1"]


def test_attendance_summary_and_full_csv_exports(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    _seed_two_slot_attendance(client)

    default_summary = client.post(
        "/api/professors/PRF002/courses/CSE116/attendance/report-exports",
        headers=auth_header("PRF002"),
    )
    assert default_summary.status_code == 201, default_summary.text
    assert api_json(default_summary)["original_filename"].startswith("attendance-summary-CSE116-")

    summary = client.post(
        "/api/professors/PRF002/courses/CSE116/attendance/report-exports",
        headers=auth_header("PRF002"),
        json={"export_type": "attendance_summary_csv"},
    )
    assert summary.status_code == 201, summary.text
    assert api_json(summary)["original_filename"].startswith("attendance-summary-CSE116-")
    summary_download = client.get(
        f"/api/professors/PRF002/courses/CSE116/attendance/report-exports/{api_json(summary)['id']}/download",
        headers=auth_header("PRF002"),
    )
    assert summary_download.status_code == 200
    summary_rows = _csv_rows(summary_download)
    assert summary_rows == [
        ["학번", "이름", "출석 차시", "결석 차시", "지각 차시", "공결 차시"],
        ["20201239", "Kim Student", "1", "0", "0", "1"],
    ]

    full = client.post(
        "/api/professors/PRF002/courses/CSE116/attendance/report-exports",
        headers=auth_header("PRF002"),
        json={"export_type": "attendance_full_csv"},
    )
    assert full.status_code == 201, full.text
    assert api_json(full)["original_filename"].startswith("attendance-full-CSE116-")
    full_download = client.get(
        f"/api/professors/PRF002/courses/CSE116/attendance/report-exports/{api_json(full)['id']}/download",
        headers=auth_header("PRF002"),
    )
    assert full_download.status_code == 200
    full_rows = _csv_rows(full_download)
    assert full_rows[0][:6] == ["학번", "이름", "출석 차시", "결석 차시", "지각 차시", "공결 차시"]
    assert len(full_rows[0]) > 6
    assert full_rows[1][:8] == ["20201239", "Kim Student", "1", "0", "0", "1", "출석", "공결"]
    assert "미진행" in full_rows[1][8:]


def test_attendance_report_export_rejects_invalid_type(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    response = client.post(
        "/api/professors/PRF002/courses/CSE116/attendance/report-exports",
        headers=auth_header("PRF002"),
        json={"export_type": "attendance_pdf"},
    )
    assert response.status_code == 422


def test_attendance_report_export_rejects_non_owner(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    owned = client.post(
        "/api/professors/PRF002/courses/CSE116/attendance/report-exports",
        headers=auth_header("PRF002"),
        json={"export_type": "attendance_summary_csv"},
    )
    assert owned.status_code == 201, owned.text
    export_id = api_json(owned)["id"]

    create_denied = client.post(
        "/api/professors/PRF003/courses/CSE116/attendance/report-exports",
        headers=auth_header("PRF003"),
        json={"export_type": "attendance_summary_csv"},
    )
    assert create_denied.status_code in {403, 404}

    download_denied = client.get(
        f"/api/professors/PRF003/courses/CSE116/attendance/report-exports/{export_id}/download",
        headers=auth_header("PRF003"),
    )
    assert download_denied.status_code in {403, 404}
