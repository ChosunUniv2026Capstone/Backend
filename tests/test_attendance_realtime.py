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
    AttendanceRecord,
    AttendanceSession,
    AttendanceSessionSlot,
    AttendanceStatusAuditLog,
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


def _open_bundle_session(client: TestClient, mode: str = "smart") -> tuple[int, list[str]]:
    first_projection_key = _first_projection_key(client)
    second_projection_key = _same_date_second_projection_key(client)
    response = client.post(
        "/api/professors/PRF002/courses/CSE116/attendance/sessions/batch",
        headers=auth_header("PRF002"),
        json={"projection_keys": [first_projection_key, second_projection_key], "mode": mode},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["changed_session_ids"]
    return payload["changed_session_ids"][0], [first_projection_key, second_projection_key]



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
    payload = response.json()
    assert all(item["success"] is True for item in payload["results"])
    assert payload["changed_session_ids"] and len(payload["changed_session_ids"]) == 1
    assert {item["session_id"] for item in payload["results"]} == {payload["changed_session_ids"][0]}
    timeline = client.get(
        "/api/professors/PRF002/courses/CSE116/attendance/timeline",
        headers=auth_header("PRF002"),
    )
    assert timeline.status_code == 200
    first_week_slots = timeline.json()["weeks"][0]["slots"][:2]
    assert {slot["session_id"] for slot in first_week_slots} == {payload["changed_session_ids"][0]}
    assert all(slot["bundle_slot_count"] == 2 for slot in first_week_slots)
    assert all(set(slot["bundle_projection_keys"]) == {first_projection_key, second_projection_key} for slot in first_week_slots)


def test_same_date_batch_open_creates_one_bundle_parent_and_memberships() -> None:
    client, session_local, _ = make_client()
    first_projection_key = _first_projection_key(client)
    second_projection_key = _same_date_second_projection_key(client)

    response = client.post(
        "/api/professors/PRF002/courses/CSE116/attendance/sessions/batch",
        headers=auth_header("PRF002"),
        json={"projection_keys": [first_projection_key, second_projection_key], "mode": "manual"},
    )

    assert response.status_code == 200
    payload = response.json()
    session_ids = {item["session_id"] for item in payload["results"]}
    assert len(session_ids) == 1
    assert payload["changed_session_ids"] == [next(iter(session_ids))]

    session_id = next(iter(session_ids))
    with session_local() as db:
        sessions = db.query(AttendanceSession).all()
        memberships = db.query(AttendanceSessionSlot).filter(AttendanceSessionSlot.attendance_session_id == session_id).all()
        assert len(sessions) == 1
        assert len(memberships) == 2
        assert {row.projection_key for row in memberships} == {first_projection_key, second_projection_key}

    timeline = client.get(
        "/api/professors/PRF002/courses/CSE116/attendance/timeline",
        headers=auth_header("PRF002"),
    )
    assert timeline.status_code == 200
    slots = timeline.json()["weeks"][0]["slots"][:2]
    assert slots[0]["session_id"] == slots[1]["session_id"] == session_id
    assert slots[0]["bundle_projection_keys"] == [first_projection_key, second_projection_key]
    assert slots[1]["bundle_projection_keys"] == [first_projection_key, second_projection_key]


def test_bundle_roster_defaults_to_anchor_absent_and_slot_preview_stays_slot_specific() -> None:
    client, _, _ = make_client()
    first_projection_key = _first_projection_key(client)
    second_projection_key = _same_date_second_projection_key(client)

    open_response = client.post(
        "/api/professors/PRF002/courses/CSE116/attendance/sessions/batch",
        headers=auth_header("PRF002"),
        json={"projection_keys": [first_projection_key, second_projection_key], "mode": "manual"},
    )
    session_id = open_response.json()["changed_session_ids"][0]

    roster = client.get(
        f"/api/professors/PRF002/attendance/sessions/{session_id}/roster",
        headers=auth_header("PRF002"),
    )
    assert roster.status_code == 200
    roster_payload = roster.json()
    assert roster_payload["session"]["projection_keys"] == [first_projection_key, second_projection_key]
    assert all(student["final_status"] == "absent" for student in roster_payload["students"])

    slot_preview = client.get(
        f"/api/professors/PRF002/courses/CSE116/attendance/slot-roster?projection_key={second_projection_key}",
        headers=auth_header("PRF002"),
    )
    assert slot_preview.status_code == 200
    preview_payload = slot_preview.json()
    assert preview_payload["session"]["session_id"] == session_id
    assert preview_payload["session"]["projection_key"] == second_projection_key
    assert all(student["final_status"] is None for student in preview_payload["students"])


def test_bundle_professor_update_fans_out_and_slot_exception_is_slot_specific() -> None:
    client, session_local, _ = make_client()
    first_projection_key = _first_projection_key(client)
    second_projection_key = _same_date_second_projection_key(client)

    open_response = client.post(
        "/api/professors/PRF002/courses/CSE116/attendance/sessions/batch",
        headers=auth_header("PRF002"),
        json={"projection_keys": [first_projection_key, second_projection_key], "mode": "manual"},
    )
    session_id = open_response.json()["changed_session_ids"][0]

    bundle_update = client.patch(
        f"/api/professors/PRF002/attendance/sessions/{session_id}/students/20201239",
        headers=auth_header("PRF002"),
        json={"status": "late", "reason": "지각 확인"},
    )
    assert bundle_update.status_code == 200
    assert bundle_update.json()["projection_keys"] == [first_projection_key, second_projection_key]

    repeat_update = client.patch(
        f"/api/professors/PRF002/attendance/sessions/{session_id}/students/20201239",
        headers=auth_header("PRF002"),
        json={"status": "late", "reason": "지각 확인"},
    )
    assert repeat_update.status_code == 200
    assert repeat_update.json()["changed"] is False

    slot_exception = client.patch(
        f"/api/professors/PRF002/attendance/sessions/{session_id}/students/20201239",
        headers=auth_header("PRF002"),
        json={"status": "official", "reason": "공가 처리", "projection_key": second_projection_key},
    )
    assert slot_exception.status_code == 200
    assert slot_exception.json()["projection_keys"] == [second_projection_key]

    with session_local() as db:
        records = (
            db.query(AttendanceRecord)
            .filter(AttendanceRecord.attendance_session_id == session_id)
            .order_by(AttendanceRecord.projection_key.asc())
            .all()
        )
        audits = (
            db.query(AttendanceStatusAuditLog)
            .filter(AttendanceStatusAuditLog.attendance_session_id == session_id)
            .order_by(AttendanceStatusAuditLog.id.asc())
            .all()
        )
        assert [(record.projection_key, record.final_status) for record in records] == [
            (first_projection_key, "late"),
            (second_projection_key, "official"),
        ]
        assert [(audit.projection_key, audit.new_status) for audit in audits] == [
            (first_projection_key, "late"),
            (second_projection_key, "late"),
            (second_projection_key, "official"),
        ]


def test_bundle_student_check_in_updates_each_slot_and_is_idempotent_per_bundle() -> None:
    client, _, _ = make_client()
    first_projection_key = _first_projection_key(client)
    second_projection_key = _same_date_second_projection_key(client)

    open_response = client.post(
        "/api/professors/PRF002/courses/CSE116/attendance/sessions/batch",
        headers=auth_header("PRF002"),
        json={"projection_keys": [first_projection_key, second_projection_key], "mode": "smart"},
    )
    session_id = open_response.json()["changed_session_ids"][0]

    first = client.post(
        f"/api/students/20201239/attendance/sessions/{session_id}/check-in",
        headers=auth_header("20201239"),
    )
    assert first.status_code == 200
    first_payload = first.json()
    assert first_payload["changed_count"] == 2
    assert first_payload["already_present_count"] == 0
    assert first_payload["rejected_count"] == 0
    assert first_payload["changed_projection_keys"] == [first_projection_key, second_projection_key]

    second = client.post(
        f"/api/students/20201239/attendance/sessions/{session_id}/check-in",
        headers=auth_header("20201239"),
    )
    assert second.status_code == 200
    second_payload = second.json()
    assert second_payload["idempotent"] is True
    assert second_payload["changed_count"] == 0
    assert second_payload["already_present_count"] == 2

    report = client.get(
        "/api/professors/PRF002/courses/CSE116/attendance/report",
        headers=auth_header("PRF002"),
    )
    assert report.status_code == 200
    assert report.json()["present"] == 2


def test_bundle_student_active_sessions_are_grouped_into_one_card() -> None:
    client, _, _ = make_client()
    session_id, projection_keys = _open_bundle_session(client, mode="smart")

    response = client.get(
        "/api/students/20201239/courses/CSE116/attendance/active-sessions",
        headers=auth_header("20201239"),
    )
    assert response.status_code == 200
    payload = response.json()
    assert len(payload["sessions"]) == 1
    session = payload["sessions"][0]
    assert session["session_id"] == session_id
    assert session["projection_keys"] == projection_keys
    assert len(session["included_slots"]) == 2
    assert session["eligibility"]["eligible_slot_count"] == 2
    assert session["can_check_in"] is True


def test_bundle_close_realtime_event_contains_all_projection_keys() -> None:
    client, _, _ = make_client()
    session_id, projection_keys = _open_bundle_session(client, mode="smart")

    with client.websocket_connect("/ws/attendance?token=dev-token:20201239&courseCode=CSE116&view=student") as websocket:
        bootstrap = websocket.receive_json()
        assert bootstrap["event_type"] == "attendance.bootstrap"
        close = client.post(
            f"/api/professors/PRF002/attendance/sessions/{session_id}/close",
            headers=auth_header("PRF002"),
        )
        assert close.status_code == 200
        event = websocket.receive_json()
        assert event["event_type"] == "attendance.session.closed"
        assert event["projection_keys"] == projection_keys


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



def test_professor_manual_update_allows_empty_reason() -> None:
    client, _, _ = make_client()
    session_id, _ = _open_session(client, mode="manual")
    response = client.patch(
        f"/api/professors/PRF002/attendance/sessions/{session_id}/students/20201239",
        headers=auth_header("PRF002"),
        json={"status": "late", "reason": ""},
    )
    assert response.status_code == 200
    assert response.json()["new_status"] == "late"
    assert response.json()["reason"] == ""



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


def test_bundle_realtime_events_publish_parent_session_with_all_projection_keys() -> None:
    client, _, _ = make_client()
    first_projection_key = _first_projection_key(client)
    second_projection_key = _same_date_second_projection_key(client)

    with client.websocket_connect("/ws/attendance?token=dev-token:PRF002&courseCode=CSE116&view=professor") as websocket:
        bootstrap = websocket.receive_json()
        assert bootstrap["event_type"] == "attendance.bootstrap"

        open_response = client.post(
            "/api/professors/PRF002/courses/CSE116/attendance/sessions/batch",
            headers=auth_header("PRF002"),
            json={"projection_keys": [first_projection_key, second_projection_key], "mode": "smart"},
        )
        assert open_response.status_code == 200
        session_id = open_response.json()["changed_session_ids"][0]

        opened_message = websocket.receive_json()
        assert opened_message["event_type"] == "attendance.session.batch_applied"
        assert opened_message["projection_keys"] == [first_projection_key, second_projection_key]
        assert opened_message["session_ids"] == [session_id]

        close_response = client.post(
            f"/api/professors/PRF002/attendance/sessions/{session_id}/close",
            headers=auth_header("PRF002"),
        )
        assert close_response.status_code == 200

        closed_message = websocket.receive_json()
        assert closed_message["event_type"] == "attendance.session.closed"
        assert closed_message["projection_keys"] == [first_projection_key, second_projection_key]
        assert closed_message["session_ids"] == [session_id]
