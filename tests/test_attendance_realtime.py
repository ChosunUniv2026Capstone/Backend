from __future__ import annotations

from collections.abc import Generator
from datetime import time
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import Session, sessionmaker

from app.db import get_db
from app.main import app
from app.models import (
    Base,
    Classroom,
    ClassroomNetwork,
    Course,
    CourseEnrollment,
    CourseSchedule,
    RegisteredDevice,
    User,
)


class FakePresenceClient:
    def check_eligibility(
        self,
        *,
        student_id: str,
        course_id: str,
        classroom_id: str,
        purpose: str,
        classroom_networks: list[dict],
        registered_devices: list[dict],
    ) -> dict:
        return {
            "eligible": True,
            "reasonCode": "OK",
            "matchedDeviceMac": registered_devices[0]["mac"],
            "observedAt": "2026-04-07T15:05:00+00:00",
            "snapshotAgeSeconds": 1,
            "evidence": {"classroomId": classroom_id, "matchedApIds": [classroom_networks[0]["apId"]]},
        }

    def get_admin_snapshot(self, *, classroom_code: str):
        raise AssertionError("admin snapshot should not be used in attendance tests")

    def apply_admin_overlay(self, *, classroom_code: str, payload: dict):
        raise AssertionError("admin overlay should not be used in attendance tests")

    def reset_admin_overlay(self, *, classroom_code: str):
        raise AssertionError("admin overlay reset should not be used in attendance tests")



def auth_header(login_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer dev-token:{login_id}"}



def make_client() -> tuple[TestClient, sessionmaker, FakePresenceClient]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    Base.metadata.create_all(engine)

    with SessionLocal.begin() as session:
        seed_backend_state(session)

    def override_get_db() -> Generator[Session, None, None]:
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    fake_presence_client = FakePresenceClient()
    app.dependency_overrides[get_db] = override_get_db

    from app import main as main_module

    main_module.presence_client = fake_presence_client
    main_module.SessionLocal = SessionLocal
    return TestClient(app), SessionLocal, fake_presence_client



def seed_backend_state(session: Session) -> None:
    student = User(student_id="20201239", name="Kim Student 06", role="student", password="devpass123")
    other_student = User(student_id="20201240", name="Kim Student 07", role="student", password="devpass123")
    professor = User(professor_id="PRF002", name="Lee Professor 02", role="professor", password="devpass123")
    other_professor = User(professor_id="PRF003", name="Park Professor 03", role="professor", password="devpass123")
    admin = User(admin_id="ADM001", name="Choi Admin 01", role="admin", password="devpass123")
    session.add_all([student, other_student, professor, other_professor, admin])
    session.flush()

    classroom = Classroom(classroom_code="B101", name="Lab", building="Main", floor_label="1F")
    other_classroom = Classroom(classroom_code="B102", name="Annex Lab", building="Main", floor_label="2F")
    session.add_all([classroom, other_classroom])
    session.flush()

    course = Course(course_code="CSE116", title="Capstone Design A", professor_user_id=professor.id)
    other_course = Course(course_code="CSE999", title="Security Testing", professor_user_id=other_professor.id)
    session.add_all([course, other_course])
    session.flush()

    session.add_all(
        [
            ClassroomNetwork(
                classroom_id=classroom.id,
                ap_id="phy3-ap0",
                ssid="CU-B101-2G-2",
                gateway_host="gw",
                signal_threshold_dbm=-65,
                collection_mode="dummy",
            ),
            ClassroomNetwork(
                classroom_id=other_classroom.id,
                ap_id="phy3-ap1",
                ssid="CU-B102-5G-1",
                gateway_host="gw-2",
                signal_threshold_dbm=-67,
                collection_mode="dummy",
            ),
        ]
    )
    session.add(
        RegisteredDevice(user_id=student.id, label="Kim Phone", mac_address="52:54:00:12:34:56", status="active")
    )
    session.add_all(
        [
            CourseEnrollment(course_id=course.id, student_user_id=student.id, status="active"),
            CourseEnrollment(course_id=course.id, student_user_id=other_student.id, status="active"),
            CourseEnrollment(course_id=other_course.id, student_user_id=other_student.id, status="active"),
            CourseSchedule(course_id=course.id, classroom_id=classroom.id, day_of_week=0, starts_at=time(15, 0), ends_at=time(16, 30)),
            CourseSchedule(course_id=other_course.id, classroom_id=other_classroom.id, day_of_week=0, starts_at=time(15, 0), ends_at=time(16, 30)),
        ]
    )



def _first_projection_key_for(client: TestClient, professor_id: str, course_code: str) -> str:
    response = client.get(
        f"/api/professors/{professor_id}/courses/{course_code}/attendance/timeline",
        headers=auth_header(professor_id),
    )
    assert response.status_code == 200
    payload = response.json()
    first_week = payload["weeks"][0]
    return first_week["slots"][0]["projection_key"]


def _first_projection_key(client: TestClient) -> str:
    return _first_projection_key_for(client, "PRF002", "CSE116")


def _same_date_second_projection_key(client: TestClient) -> str:
    response = client.get(
        "/api/professors/PRF002/courses/CSE116/attendance/timeline",
        headers=auth_header("PRF002"),
    )
    assert response.status_code == 200
    payload = response.json()
    return payload["weeks"][0]["slots"][1]["projection_key"]



def _open_session_for(client: TestClient, professor_id: str, course_code: str, mode: str = "smart") -> tuple[int, str]:
    projection_key = _first_projection_key_for(client, professor_id, course_code)
    response = client.post(
        f"/api/professors/{professor_id}/courses/{course_code}/attendance/sessions/batch",
        headers=auth_header(professor_id),
        json={"projection_keys": [projection_key], "mode": mode},
    )
    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["success"] is True
    return result["session_id"], projection_key


def _open_session(client: TestClient, mode: str = "smart") -> tuple[int, str]:
    return _open_session_for(client, "PRF002", "CSE116", mode)



def test_professor_timeline_returns_semester_slots() -> None:
    client, _, _ = make_client()
    response = client.get(
        "/api/professors/PRF002/courses/CSE116/attendance/timeline",
        headers=auth_header("PRF002"),
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["course_code"] == "CSE116"
    assert payload["weeks"]
    assert payload["weeks"][0]["slots"][0]["display_label"]



def test_batch_open_reports_partial_duplicate_failure() -> None:
    client, _, _ = make_client()
    projection_key = _first_projection_key(client)
    first = client.post(
        "/api/professors/PRF002/courses/CSE116/attendance/sessions/batch",
        headers=auth_header("PRF002"),
        json={"projection_keys": [projection_key], "mode": "manual"},
    )
    assert first.status_code == 200

    second = client.post(
        "/api/professors/PRF002/courses/CSE116/attendance/sessions/batch",
        headers=auth_header("PRF002"),
        json={"projection_keys": [projection_key, "CSE116:B101:2026-03-03:15:00:00:15:30:00"], "mode": "manual"},
    )
    assert second.status_code == 200
    results = second.json()["results"]
    assert results[0]["code"] == "SESSION_ALREADY_OPEN"
    assert results[1]["code"] == "SESSION_SLOT_INVALID"


def test_batch_open_rejects_cross_date_selection() -> None:
    client, _, _ = make_client()
    projection_key = _first_projection_key(client)
    later_projection_key = "CSE116:B101:2026-03-11:12:00:00:12:30:00"
    response = client.post(
        "/api/professors/PRF002/courses/CSE116/attendance/sessions/batch",
        headers=auth_header("PRF002"),
        json={"projection_keys": [projection_key, later_projection_key], "mode": "manual"},
    )
    assert response.status_code == 200
    results = response.json()["results"]
    assert results[0]["success"] is True
    assert results[1]["code"] == "SESSION_SLOT_INVALID"


def test_batch_open_accepts_same_date_multiple_slots() -> None:
    client, _, _ = make_client()
    first_projection_key = _first_projection_key(client)
    second_projection_key = _same_date_second_projection_key(client)
    response = client.post(
        "/api/professors/PRF002/courses/CSE116/attendance/sessions/batch",
        headers=auth_header("PRF002"),
        json={"projection_keys": [first_projection_key, second_projection_key], "mode": "manual"},
    )
    assert response.status_code == 200
    assert all(item["success"] is True for item in response.json()["results"])



def test_student_check_in_is_idempotent_and_updates_report() -> None:
    client, _, _ = make_client()
    session_id, _ = _open_session(client, mode="smart")

    first = client.post(
        f"/api/students/20201239/attendance/sessions/{session_id}/check-in",
        headers=auth_header("20201239"),
    )
    assert first.status_code == 200
    assert first.json()["idempotent"] is False

    second = client.post(
        f"/api/students/20201239/attendance/sessions/{session_id}/check-in",
        headers=auth_header("20201239"),
    )
    assert second.status_code == 200
    assert second.json()["idempotent"] is True

    report = client.get(
        "/api/professors/PRF002/courses/CSE116/attendance/report",
        headers=auth_header("PRF002"),
    )
    assert report.status_code == 200
    assert report.json()["present"] == 1



def test_professor_manual_update_requires_reason() -> None:
    client, _, _ = make_client()
    session_id, _ = _open_session(client, mode="manual")
    response = client.patch(
        f"/api/professors/PRF002/attendance/sessions/{session_id}/students/20201239",
        headers=auth_header("PRF002"),
        json={"status": "late", "reason": ""},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "ATTENDANCE_REASON_REQUIRED"



def test_professor_override_updates_history_and_report() -> None:
    client, _, _ = make_client()
    session_id, _ = _open_session(client, mode="smart")
    client.post(
        f"/api/students/20201239/attendance/sessions/{session_id}/check-in",
        headers=auth_header("20201239"),
    )
    override = client.patch(
        f"/api/professors/PRF002/attendance/sessions/{session_id}/students/20201239",
        headers=auth_header("PRF002"),
        json={"status": "late", "reason": "지각 확인"},
    )
    assert override.status_code == 200

    history = client.get(
        "/api/professors/PRF002/courses/CSE116/attendance/students/20201239/history",
        headers=auth_header("PRF002"),
    )
    assert history.status_code == 200
    assert len(history.json()["entries"]) == 2
    assert history.json()["entries"][0]["new_status"] == "late"

    report = client.get(
        "/api/professors/PRF002/courses/CSE116/attendance/report",
        headers=auth_header("PRF002"),
    )
    assert report.status_code == 200
    assert report.json()["late"] == 1
    assert report.json()["present"] == 0


def test_canceled_session_blocks_check_in_until_reopened() -> None:
    client, _, _ = make_client()
    projection_key = _first_projection_key(client)
    cancel = client.post(
        "/api/professors/PRF002/courses/CSE116/attendance/sessions/batch",
        headers=auth_header("PRF002"),
        json={"projection_keys": [projection_key], "mode": "canceled"},
    )
    assert cancel.status_code == 200

    sessions = client.get(
        "/api/students/20201239/courses/CSE116/attendance/active-sessions",
        headers=auth_header("20201239"),
    )
    assert sessions.status_code == 200
    assert sessions.json()["sessions"] == []

    reopen = client.post(
        "/api/professors/PRF002/courses/CSE116/attendance/sessions/batch",
        headers=auth_header("PRF002"),
        json={"projection_keys": [projection_key], "mode": "smart"},
    )
    assert reopen.status_code == 200
    assert reopen.json()["results"][0]["event_type"] == "session.reopened"


def test_student_active_sessions_read_is_time_independent() -> None:
    client, _, _ = make_client()
    response = client.get(
        "/api/students/20201239/courses/CSE116/attendance/active-sessions",
        headers=auth_header("20201239"),
    )
    assert response.status_code == 200
    assert response.json()["sessions"] == []


def test_student_active_sessions_reject_non_enrolled_course() -> None:
    client, _, _ = make_client()
    response = client.get(
        "/api/students/20201239/courses/CSE999/attendance/active-sessions",
        headers=auth_header("20201239"),
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "FORBIDDEN"


def test_slot_roster_preview_returns_students_without_session() -> None:
    client, _, _ = make_client()
    projection_key = _first_projection_key(client)
    response = client.get(
        f"/api/professors/PRF002/courses/CSE116/attendance/slot-roster?projection_key={projection_key}",
        headers=auth_header("PRF002"),
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["session"]["projection_key"] == projection_key
    assert len(payload["students"]) == 2


def test_professor_timeline_rejects_non_owned_course() -> None:
    client, _, _ = make_client()
    response = client.get(
        "/api/professors/PRF002/courses/CSE999/attendance/timeline",
        headers=auth_header("PRF002"),
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "FORBIDDEN"


def test_professor_slot_roster_rejects_non_owned_course() -> None:
    client, _, _ = make_client()
    projection_key = _first_projection_key_for(client, "PRF003", "CSE999")
    response = client.get(
        f"/api/professors/PRF002/courses/CSE999/attendance/slot-roster?projection_key={projection_key}",
        headers=auth_header("PRF002"),
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "FORBIDDEN"


def test_student_check_in_rejects_session_for_non_enrolled_course() -> None:
    client, _, _ = make_client()
    session_id, _ = _open_session_for(client, "PRF003", "CSE999", mode="smart")
    response = client.post(
        f"/api/students/20201239/attendance/sessions/{session_id}/check-in",
        headers=auth_header("20201239"),
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "FORBIDDEN"



def test_websocket_rejects_unauthorized_student_subscription() -> None:
    client, _, _ = make_client()
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/attendance?token=dev-token:ADM001&courseCode=CSE116&view=student"):
            pass


def test_websocket_rejects_non_enrolled_student_subscription() -> None:
    client, _, _ = make_client()
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/attendance?token=dev-token:20201239&courseCode=CSE999&view=student"):
            pass


def test_websocket_rejects_wrong_professor_subscription() -> None:
    client, _, _ = make_client()
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/attendance?token=dev-token:PRF002&courseCode=CSE999&view=professor"):
            pass


def test_professor_websocket_bootstrap_delivers_timeline() -> None:
    client, _, _ = make_client()
    with client.websocket_connect("/ws/attendance?token=dev-token:PRF002&courseCode=CSE116&view=professor") as websocket:
        message = websocket.receive_json()
        assert message["event_type"] == "attendance.bootstrap"
        assert message["changed_payload"]["data"]["course_code"] == "CSE116"


def test_admin_report_websocket_bootstrap_is_allowed() -> None:
    client, _, _ = make_client()
    with client.websocket_connect("/ws/attendance?token=dev-token:ADM001&courseCode=CSE116&view=report") as websocket:
        message = websocket.receive_json()
        assert message["event_type"] == "attendance.bootstrap"
        assert message["changed_payload"]["view"] == "report"
        assert message["changed_payload"]["data"]["course_code"] == "CSE116"
