from __future__ import annotations
from envelope import api_json

from collections.abc import Generator

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import Session, sessionmaker

from app.auth import issue_access_token
from app.db import get_db
from app.main import app
from app.presence_client import PresenceClient
import app.services as services_module
from app.models import AccessPoint, AccessPointInterface, Base, Classroom, ClassroomNetwork, Course, CourseEnrollment, CourseSchedule, Notice, PresenceEligibilityLog, RegisteredDevice, User

from datetime import datetime, timedelta


class FakePresenceClient:
    def __init__(self) -> None:
        self.last_overlay_payload = None
        self.last_eligibility_payload = None
        self.last_admin_refresh = None
        self.last_admin_source = None
        self.next_eligibility_response = None
        self.reason_code = "OK"

    def get_admin_snapshot(self, *, classroom_code: str, refresh: bool = False, source: str = "auto"):
        assert classroom_code == "B101"
        self.last_admin_refresh = refresh
        self.last_admin_source = source
        return {
            "cacheHit": False,
            "overlayActive": True,
            "snapshot": {
                "classroomId": "B101",
                "observedAt": "2026-04-07T15:05:00+09:00",
                "collectionMode": "dummy-openwrt",
                "aps": [
                    {
                        "apId": "phy3-ap0",
                        "ssid": "CU-B101-2G-2",
                        "sourceCommand": "iw dev phy3-ap0 station dump",
                        "stations": [
                            {
                                "macAddress": "52:54:00:12:34:56",
                                "authorized": True,
                                "authenticated": True,
                                "associated": True,
                                "signalDbm": -47,
                                "connectedSeconds": 95,
                                "rxBytes": 120101,
                                "txBytes": 94310,
                            }
                        ],
                    }
                ],
            },
        }

    def apply_admin_overlay(self, *, classroom_code: str, payload: dict):
        self.last_overlay_payload = payload
        return self.get_admin_snapshot(classroom_code=classroom_code)

    def reset_admin_overlay(self, *, classroom_code: str):
        payload = self.get_admin_snapshot(classroom_code=classroom_code)
        payload["overlayActive"] = False
        return payload

    def check_eligibility(
        self,
        *,
        student_id: str,
        course_id: str,
        classroom_id: str,
        purpose: str,
        classroom_networks: list[dict],
        registered_devices: list[dict],
    ):
        self.last_eligibility_payload = {
            "student_id": student_id,
            "course_id": course_id,
            "classroom_id": classroom_id,
            "purpose": purpose,
            "classroom_networks": classroom_networks,
            "registered_devices": registered_devices,
        }
        assert student_id == "20201239"
        assert course_id == "CSE116"
        assert classroom_id == "B101"
        assert classroom_networks[0]["apId"] == "phy3-ap0"
        if self.next_eligibility_response is not None:
            return self.next_eligibility_response
        return {
            "eligible": self.reason_code == "OK",
            "reasonCode": self.reason_code,
            "matchedDeviceMac": registered_devices[0]["mac"],
            "observedAt": "2026-04-07T15:05:00+09:00",
            "snapshotAgeSeconds": 2,
            "evidence": {"classroomId": classroom_id, "matchedApIds": ["phy3-ap0"]},
        }


def make_client() -> tuple[TestClient, FakePresenceClient]:
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
    return TestClient(app), fake_presence_client


def seed_backend_state(session: Session) -> None:
    student = User(student_id="20201239", name="Kim Student 06", role="student", password="devpass123")
    other_student = User(student_id="20201234", name="Kim Student 01", role="student", password="devpass123")
    professor = User(professor_id="PRF002", name="Lee Professor 02", role="professor", password="devpass123")
    admin = User(admin_id="ADM001", name="Choi Admin 01", role="admin", password="devpass123")
    session.add_all([student, other_student, professor, admin])
    session.flush()

    classroom = Classroom(classroom_code="B101", name="Lab", building="Main", floor_label="1F")
    session.add(classroom)
    session.flush()

    course = Course(course_code="CSE116", title="Capstone Design A", professor_user_id=professor.id)
    session.add(course)
    session.flush()

    network = ClassroomNetwork(
        classroom_id=classroom.id,
        ap_id="phy3-ap0",
        ssid="CU-B101-2G-2",
        gateway_host="gw",
        collection_mode="dummy",
    )
    device = RegisteredDevice(user_id=student.id, label="Choi Phone", mac_address="52:54:00:12:34:56", status="active")
    enrollment = CourseEnrollment(course_id=course.id, student_user_id=student.id, status="active")
    now = datetime.now()
    start_time = (now - timedelta(minutes=30)).time().replace(microsecond=0)
    end_time = (now + timedelta(minutes=30)).time().replace(microsecond=0)
    schedule = CourseSchedule(
        course_id=course.id,
        classroom_id=classroom.id,
        day_of_week=now.weekday(),
        starts_at=start_time,
        ends_at=end_time,
    )
    session.add_all([network, device, enrollment, schedule])
    session.flush()
    access_point = AccessPoint(collector_ap_id="openwrt-a", label="Demo AP A / B101", management_ip="192.168.97.1", tailnet_ip="100.78.116.89", status="active")
    session.add(access_point)
    session.flush()
    session.add(AccessPointInterface(access_point_id=access_point.id, interface_id="phy3-ap0", ssid="CU-B101-2G-2", classroom_network_id=network.id))




def test_student_can_re_register_deleted_device() -> None:
    client, _ = make_client()
    devices = client.get("/api/students/20201239/devices", headers=auth_header("20201239"))
    assert devices.status_code == 200
    device_id = api_json(devices)[0]["id"]

    deleted = client.delete(f"/api/students/20201239/devices/{device_id}", headers=auth_header("20201239"))
    assert deleted.status_code == 204
    assert deleted.content == b""

    recreated = client.post(
        "/api/students/20201239/devices",
        headers=auth_header("20201239"),
        json={"label": "Recovered Phone", "mac_address": "52:54:00:12:34:56"},
    )

    assert recreated.status_code == 201
    assert api_json(recreated)["id"] == device_id
    assert api_json(recreated)["label"] == "Recovered Phone"
    assert api_json(recreated)["status"] == "active"


def test_api_success_responses_are_enveloped() -> None:
    client, _ = make_client()

    response = client.get("/api/admin/users", headers=auth_header("ADM001"))

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert isinstance(payload["data"], list)
    assert payload["data"][0]["role"] in {"student", "professor", "admin"}
    assert payload["message"] == "ok"
    assert payload["meta"] == {}
    assert "detail" not in payload


def test_api_http_errors_are_enveloped_with_stable_codes() -> None:
    client, _ = make_client()

    unauthenticated = client.get("/api/admin/users")
    forbidden = client.get("/api/admin/users", headers=auth_header("20201239"))

    assert unauthenticated.status_code == 401
    assert unauthenticated.json() == {
        "success": False,
        "error": {
            "code": "UNAUTHENTICATED",
            "message": "authentication is required",
            "details": {},
        },
    }
    assert forbidden.status_code == 403
    assert forbidden.json() == {
        "success": False,
        "error": {
            "code": "FORBIDDEN",
            "message": "admin role is required",
            "details": {},
        },
    }


def test_api_validation_errors_are_enveloped_with_stable_code() -> None:
    client, _ = make_client()

    response = client.post("/api/auth/login", json={})

    assert response.status_code == 422
    payload = response.json()
    assert payload["success"] is False
    assert payload["error"]["code"] == "VALIDATION_ERROR"
    assert payload["error"]["message"] == "request validation failed"
    assert payload["error"]["details"]["errors"]


def test_student_notice_list_includes_common_notices() -> None:
    client, _ = make_client()
    from app.db import get_db as backend_get_db
    from app.main import app as backend_app

    override = backend_app.dependency_overrides[backend_get_db]
    db = next(override())
    try:
        professor = db.scalar(select(User).where(User.professor_id == "PRF002"))
        db.add(Notice(author_user_id=professor.id, course_id=None, title="Common Notice", body="Everyone reads this"))
        db.commit()
    finally:
        db.close()

    response = client.get("/api/notices/20201239", headers=auth_header("20201239"))

    assert response.status_code == 200
    common = next(item for item in api_json(response)["data"] if item["title"] == "Common Notice")
    assert common["course_code"] is None


def test_presence_client_maps_upstream_validation_error_to_http_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(*args, **kwargs):
        request = httpx.Request("POST", "http://presence/admin/dummy/classrooms/B101/overlay")
        response = httpx.Response(
            400,
            request=request,
            json={"detail": {"code": "INVALID_AP_ID", "message": "invalid ap id", "details": {"apId": "demo-ap"}}},
        )
        return response

    monkeypatch.setattr(httpx, "post", fake_post)

    with pytest.raises(Exception) as exc_info:
        PresenceClient("http://presence").apply_admin_overlay(classroom_code="B101", payload={"stations": []})

    exc = exc_info.value
    assert getattr(exc, "status_code", None) == 400
    assert exc.detail["code"] == "INVALID_AP_ID"
    assert exc.detail["details"] == {"apId": "demo-ap"}

def test_admin_presence_snapshot_dropdown_includes_registered_union() -> None:
    client, _ = make_client()
    response = client.get("/api/admin/presence/classrooms/B101/snapshot", headers=auth_header("ADM001"))
    assert response.status_code == 200
    payload = api_json(response)
    option = next(item for item in payload["deviceOptions"] if item["macAddress"] == "52:54:00:12:34:56")
    assert option["studentLoginId"] == "20201239"
    assert option["deviceLabel"] == "Choi Phone"


def test_admin_presence_snapshot_dropdown_ignores_current_schedule_window() -> None:
    client, _ = make_client()
    from app.db import get_db as backend_get_db
    from app.main import app as backend_app

    override = backend_app.dependency_overrides[backend_get_db]
    db = next(override())
    try:
        schedule = db.scalar(select(CourseSchedule).join(Course).where(Course.course_code == "CSE116"))
        schedule.day_of_week = (datetime.now().weekday() + 3) % 7
        db.commit()
    finally:
        db.close()

    response = client.get("/api/admin/presence/classrooms/B101/snapshot", headers=auth_header("ADM001"))

    assert response.status_code == 200
    option = next(item for item in api_json(response)["deviceOptions"] if item["macAddress"] == "52:54:00:12:34:56")
    assert option["studentLoginId"] == "20201239"
    assert option["studentName"] == "Kim Student 06"


def test_generic_attendance_eligibility_uses_course_classroom_outside_window() -> None:
    client, fake_presence_client = make_client()
    from app.db import get_db as backend_get_db
    from app.main import app as backend_app

    override = backend_app.dependency_overrides[backend_get_db]
    db = next(override())
    try:
        schedule = db.scalar(select(CourseSchedule).join(Course).where(Course.course_code == "CSE116"))
        schedule.day_of_week = (datetime.now().weekday() + 1) % 7
        db.commit()
    finally:
        db.close()

    response = client.post(
        "/api/attendance/eligibility",
        headers=auth_header("20201239"),
        json={"student_id": "20201239", "course_code": "CSE116"},
    )
    assert response.status_code == 200
    assert api_json(response)["eligible"] is True
    assert api_json(response)["reason_code"] == "OK"
    assert fake_presence_client.last_eligibility_payload["classroom_id"] == "B101"


def test_generic_attendance_eligibility_uses_mapping_for_overnight_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_now = datetime(2026, 5, 14, 0, 5, 0)

    class _FixedDateTime:
        @staticmethod
        def now() -> datetime:
            return fixed_now

    client, fake_presence_client = make_client()
    from app.db import get_db as backend_get_db
    from app.main import app as backend_app

    override = backend_app.dependency_overrides[backend_get_db]
    db = next(override())
    try:
        schedule = db.scalar(select(CourseSchedule).join(Course).where(Course.course_code == "CSE116"))
        schedule.day_of_week = (fixed_now.weekday() - 1) % 7
        schedule.starts_at = (fixed_now - timedelta(minutes=35)).time().replace(microsecond=0)
        schedule.ends_at = (fixed_now + timedelta(minutes=25)).time().replace(microsecond=0)
        db.commit()
    finally:
        db.close()

    monkeypatch.setattr(services_module, "datetime", _FixedDateTime)
    response = client.post(
        "/api/attendance/eligibility",
        headers=auth_header("20201239"),
        json={"student_id": "20201239", "course_code": "CSE116"},
    )

    assert response.status_code == 200
    assert api_json(response)["eligible"] is True
    assert fake_presence_client.last_eligibility_payload["classroom_id"] == "B101"


def test_generic_attendance_eligibility_uses_mapping_for_future_same_day_overnight_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_now = datetime(2026, 5, 14, 0, 5, 0)

    class _FixedDateTime:
        @staticmethod
        def now() -> datetime:
            return fixed_now

    client, fake_presence_client = make_client()
    from app.db import get_db as backend_get_db
    from app.main import app as backend_app

    override = backend_app.dependency_overrides[backend_get_db]
    db = next(override())
    try:
        schedule = db.scalar(select(CourseSchedule).join(Course).where(Course.course_code == "CSE116"))
        schedule.day_of_week = fixed_now.weekday()
        schedule.starts_at = (fixed_now + timedelta(hours=23, minutes=25)).time().replace(microsecond=0)
        schedule.ends_at = (fixed_now + timedelta(minutes=55)).time().replace(microsecond=0)
        db.commit()
    finally:
        db.close()

    monkeypatch.setattr(services_module, "datetime", _FixedDateTime)
    response = client.post(
        "/api/attendance/eligibility",
        headers=auth_header("20201239"),
        json={"student_id": "20201239", "course_code": "CSE116"},
    )

    assert response.status_code == 200
    assert api_json(response)["eligible"] is True
    assert api_json(response)["reason_code"] == "OK"
    assert fake_presence_client.last_eligibility_payload["classroom_id"] == "B101"


def test_resolve_active_classroom_conflict_returns_not_eligible() -> None:
    client, _ = make_client()
    from app.db import get_db as backend_get_db
    from app.main import app as backend_app

    override = backend_app.dependency_overrides[backend_get_db]
    db = next(override())
    try:
        db.add(Classroom(classroom_code="B102", name="Other", building="Main", floor_label="2F"))
        db.flush()
        course = db.scalar(select(Course).where(Course.course_code == "CSE116"))
        classroom = db.scalar(select(Classroom).where(Classroom.classroom_code == "B102"))
        now = datetime.now()
        db.add(
            CourseSchedule(
                course_id=course.id,
                classroom_id=classroom.id,
                day_of_week=now.weekday(),
                starts_at=(now - timedelta(minutes=30)).time().replace(microsecond=0),
                ends_at=(now + timedelta(minutes=30)).time().replace(microsecond=0),
            )
        )
        db.commit()
    finally:
        db.close()
    response = client.post(
        "/api/attendance/eligibility",
        headers=auth_header("20201239"),
        json={"student_id": "20201239", "course_code": "CSE116"},
    )
    assert response.status_code == 200
    assert api_json(response)["reason_code"] == "CLASSROOM_CONFLICT"


def auth_header(login_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer dev-token:{login_id}"}


def test_admin_routes_require_admin_role() -> None:
    client, _ = make_client()
    assert client.get("/api/admin/users").status_code == 401
    assert client.get("/api/admin/users", headers=auth_header("20201239")).status_code == 403
    response = client.get("/api/admin/users", headers=auth_header("ADM001"))
    assert response.status_code == 200
    assert len(api_json(response)) == 4


def test_invalid_access_token_returns_stable_auth_code() -> None:
    client, _ = make_client()
    response = client.get("/api/admin/users", headers={"Authorization": "Bearer not-a-dev-token"})
    assert response.status_code == 401
    assert api_json(response)["detail"]["code"] == "UNAUTHENTICATED"


def test_student_routes_require_self() -> None:
    client, _ = make_client()
    own_response = client.get("/api/students/20201239/courses", headers=auth_header("20201239"))
    assert own_response.status_code == 200
    other_response = client.get("/api/students/20201239/courses", headers=auth_header("20201234"))
    assert other_response.status_code == 403


def test_admin_presence_snapshot_enriches_owner_data() -> None:
    client, _ = make_client()
    response = client.get("/api/admin/presence/classrooms/B101/snapshot", headers=auth_header("ADM001"))
    assert response.status_code == 200
    payload = api_json(response)
    assert payload["classroomCode"] == "B101"
    assert payload["classroomNetworks"][0]["signal_threshold_dbm"] is None
    station = payload["aps"][0]["stations"][0]
    assert station["ownerLoginId"] == "20201239"
    assert station["ownerName"] == "Kim Student 06"
    assert station["deviceLabel"] == "Choi Phone"
    assert any(option["macAddress"] == "52:54:00:12:34:56" for option in payload["deviceOptions"])


def test_admin_presence_snapshot_forwards_refresh_flag() -> None:
    client, fake_presence = make_client()
    response = client.get("/api/admin/presence/classrooms/B101/snapshot?refresh=true&source=demo", headers=auth_header("ADM001"))
    assert response.status_code == 200
    assert fake_presence.last_admin_refresh is True
    assert fake_presence.last_admin_source == "demo"


def test_login_sets_refresh_cookie_and_bootstraps_with_cookie_restore() -> None:
    client, _ = make_client()
    login = client.post("/api/auth/login", json={"login_id": "20201239", "password": "devpass123"})
    assert login.status_code == 200
    payload = api_json(login)
    assert payload["success"] is True
    assert payload["data"]["user"]["login_id"] == "20201239"
    assert payload["data"]["access_token"].count(".") == 2
    assert payload["access_token"] == payload["data"]["access_token"]
    assert login.cookies.get("smartclass_access")
    assert login.cookies.get("smartclass_refresh")
    set_cookie = login.headers.get("set-cookie", "")
    assert "HttpOnly" in set_cookie
    assert "Path=/api/auth" in set_cookie
    assert "SameSite=lax" in set_cookie

    bootstrap = client.get("/api/auth/bootstrap")
    assert bootstrap.status_code == 200
    bootstrap_payload = api_json(bootstrap)
    assert bootstrap_payload["success"] is True
    assert bootstrap_payload["meta"]["restored_via"] == "access-cookie"
    assert bootstrap_payload["data"]["user"]["login_id"] == "20201239"
    assert "CSE116" in bootstrap_payload["data"]["route_access"]["student_course_codes"]


def test_cookie_backed_access_allows_protected_route_without_authorization_header() -> None:
    client, _ = make_client()
    login = client.post("/api/auth/login", json={"login_id": "20201239", "password": "devpass123"})
    assert login.status_code == 200

    response = client.get("/api/students/20201239/courses")
    assert response.status_code == 200
    assert any(course["course_code"] == "CSE116" for course in api_json(response))


def test_professor_courses_are_deduplicated_when_multiple_schedule_rows_exist() -> None:
    client, _ = make_client()
    from app.db import get_db as backend_get_db
    from app.main import app as backend_app

    override = backend_app.dependency_overrides[backend_get_db]
    db = next(override())
    try:
        course = db.scalar(select(Course).where(Course.course_code == "CSE116"))
        classroom = db.scalar(select(Classroom).where(Classroom.classroom_code == "B101"))
        db.add(
            CourseSchedule(
                course_id=course.id,
                classroom_id=classroom.id,
                day_of_week=(datetime.now().weekday() + 2) % 7,
                starts_at=(datetime.now() - timedelta(minutes=90)).time().replace(microsecond=0),
                ends_at=(datetime.now() - timedelta(minutes=30)).time().replace(microsecond=0),
            )
        )
        db.commit()
    finally:
        db.close()

    response = client.get("/api/professors/PRF002/courses", headers=auth_header("PRF002"))
    assert response.status_code == 200
    course_codes = [course["course_code"] for course in api_json(response)]
    assert course_codes.count("CSE116") == 1


def test_student_courses_are_deduplicated_when_multiple_schedule_rows_exist() -> None:
    client, _ = make_client()
    from app.db import get_db as backend_get_db
    from app.main import app as backend_app

    override = backend_app.dependency_overrides[backend_get_db]
    db = next(override())
    try:
        course = db.scalar(select(Course).where(Course.course_code == "CSE116"))
        classroom = db.scalar(select(Classroom).where(Classroom.classroom_code == "B101"))
        db.add(
            CourseSchedule(
                course_id=course.id,
                classroom_id=classroom.id,
                day_of_week=(datetime.now().weekday() + 3) % 7,
                starts_at=(datetime.now() - timedelta(minutes=120)).time().replace(microsecond=0),
                ends_at=(datetime.now() - timedelta(minutes=60)).time().replace(microsecond=0),
            )
        )
        db.commit()
    finally:
        db.close()

    response = client.get("/api/students/20201239/courses", headers=auth_header("20201239"))
    assert response.status_code == 200
    course_codes = [course["course_code"] for course in api_json(response)]
    assert course_codes.count("CSE116") == 1


def test_refresh_rotates_cookie_and_rejects_replay() -> None:
    client, _ = make_client()
    login = client.post("/api/auth/login", json={"login_id": "20201239", "password": "devpass123"})
    first_refresh_cookie = login.cookies.get("smartclass_refresh")
    assert first_refresh_cookie

    refresh = client.post("/api/auth/refresh")
    assert refresh.status_code == 200
    refreshed_cookie = refresh.cookies.get("smartclass_refresh")
    assert refreshed_cookie
    assert refreshed_cookie != first_refresh_cookie

    client.cookies.set("smartclass_refresh", first_refresh_cookie, domain="testserver.local", path="/api/auth")
    replay = client.post("/api/auth/refresh")
    assert replay.status_code == 401
    assert api_json(replay)["error"]["code"] == "REFRESH_REPLAY_DETECTED"


def test_local_dev_cors_allows_credentials_for_auth_routes() -> None:
    client, _ = make_client()
    response = client.options(
        "/api/auth/refresh",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert response.headers["access-control-allow-credentials"] == "true"


def test_logout_revokes_cookie_backed_restore() -> None:
    client, _ = make_client()
    login = client.post("/api/auth/login", json={"login_id": "20201239", "password": "devpass123"})
    assert login.status_code == 200

    logout = client.post("/api/auth/logout")
    assert logout.status_code == 200
    assert api_json(logout)["data"]["logged_out"] is True

    bootstrap = client.get("/api/auth/bootstrap")
    assert bootstrap.status_code == 401
    assert api_json(bootstrap)["error"]["code"] == "UNAUTHENTICATED"


def test_expired_access_token_returns_stable_code() -> None:
    client, _ = make_client()
    from app.db import get_db as backend_get_db
    from app.main import app as backend_app

    override = backend_app.dependency_overrides[backend_get_db]
    db = next(override())
    try:
        student = db.scalar(select(User).where(User.student_id == "20201239"))
        expired_access = issue_access_token(student, ttl_seconds=-5).token
    finally:
        db.close()

    response = client.get(
        "/api/students/20201239/courses",
        headers={"Authorization": f"Bearer {expired_access}"},
    )
    assert response.status_code == 401
    assert api_json(response)["detail"]["code"] == "TOKEN_EXPIRED"


def test_invalid_access_token_returns_stable_unauthenticated_code() -> None:
    client, _ = make_client()
    response = client.get(
        "/api/students/20201239/courses",
        headers={"Authorization": "Bearer not-a-valid-token"},
    )
    assert response.status_code == 401
    assert api_json(response)["detail"]["code"] == "UNAUTHENTICATED"


def test_attendance_bootstrap_rejects_unauthorized_course_routes() -> None:
    client, _ = make_client()
    from app.db import get_db as backend_get_db
    from app.main import app as backend_app

    override = backend_app.dependency_overrides[backend_get_db]
    db = next(override())
    try:
        other_professor = User(professor_id="PRF777", name="Other Professor", role="professor", password="devpass123")
        db.add(other_professor)
        db.flush()
        db.add(Course(course_code="CSE999", title="Other Course", professor_user_id=other_professor.id))
        db.commit()
    finally:
        db.close()

    student_bootstrap = client.get(
        "/api/students/20201239/courses/CSE999/attendance/bootstrap",
        headers=auth_header("20201239"),
    )
    assert student_bootstrap.status_code == 403
    assert api_json(student_bootstrap)["detail"]["code"] == "COURSE_ROUTE_FORBIDDEN"

    professor_bootstrap = client.get(
        "/api/professors/PRF002/courses/CSE999/attendance/bootstrap",
        headers=auth_header("PRF002"),
    )
    assert professor_bootstrap.status_code == 403
    assert api_json(professor_bootstrap)["detail"]["code"] == "COURSE_ROUTE_FORBIDDEN"


def test_admin_presence_overlay_proxies_payload() -> None:
    client, fake_presence_client = make_client()
    response = client.post(
        "/api/admin/presence/classrooms/B101/dummy-controls",
        headers=auth_header("ADM001"),
        json={
            "stations": [
                {
                    "macAddress": "52:54:00:12:34:56",
                    "apId": "phy3-ap0",
                    "present": False,
                }
            ]
        },
    )
    assert response.status_code == 200
    assert fake_presence_client.last_overlay_payload == {
        "stations": [
            {
                "macAddress": "52:54:00:12:34:56",
                "apId": "phy3-ap0",
                "present": False,
                "associated": None,
                "authorized": None,
                "authenticated": None,
                "signalDbm": None,
                "connectedSeconds": None,
                "rxBytes": None,
                "txBytes": None,
            }
        ]
    }


def test_generic_attendance_eligibility_requires_student_self() -> None:
    client, fake_presence_client = make_client()
    forbidden = client.post(
        "/api/attendance/eligibility",
        headers=auth_header("ADM001"),
        json={
            "student_id": "20201239",
            "course_code": "CSE116",
        },
    )
    assert forbidden.status_code == 403

    allowed = client.post(
        "/api/attendance/eligibility",
        headers=auth_header("20201239"),
        json={
            "student_id": "20201239",
            "course_code": "CSE116",
        },
    )
    assert allowed.status_code == 200
    assert api_json(allowed)["eligible"] is True
    assert fake_presence_client.last_eligibility_payload["classroom_id"] == "B101"


def test_generic_attendance_eligibility_resolves_classroom_from_course_mapping() -> None:
    client, fake_presence_client = make_client()
    response = client.post(
        "/api/attendance/eligibility",
        headers=auth_header("20201239"),
        json={
            "student_id": "20201239",
            "course_code": "CSE116",
        },
    )
    assert response.status_code == 200
    assert fake_presence_client.last_eligibility_payload["classroom_id"] == "B101"


def test_admin_can_update_network_threshold() -> None:
    client, _ = make_client()
    response = client.patch(
        "/api/admin/classroom-networks/1",
        headers=auth_header("ADM001"),
        json={"signal_threshold_dbm": -55},
    )
    assert response.status_code == 200
    assert api_json(response)["signal_threshold_dbm"] == -55


def test_admin_can_issue_ap_token_and_internal_registry_exposes_hash_only() -> None:
    client, _ = make_client()
    issued = client.post("/api/admin/access-points/openwrt-a/token", headers=auth_header("ADM001"))
    assert issued.status_code == 200
    token_payload = api_json(issued)["data"]
    assert token_payload["collector_ap_id"] == "openwrt-a"
    assert token_payload["token"]

    registry = client.get(
        "/api/internal/presence/ap-registry",
        headers={"X-Internal-Token": "smart-class-dev-internal-token"},
    )
    assert registry.status_code == 200
    ap = api_json(registry)["accessPoints"][0]
    assert ap["collectorApId"] == "openwrt-a"
    assert ap["tokenHash"]
    assert ap["tokenHash"] != token_payload["token"]
    assert ap["interfaces"][0]["classroomId"] == "B101"
    assert ap["interfaces"][0]["classroomNetworkApId"] == "phy3-ap0"


def test_admin_can_revoke_ap_token() -> None:
    client, _ = make_client()
    assert client.post("/api/admin/access-points/openwrt-a/token", headers=auth_header("ADM001")).status_code == 200
    revoked = client.delete("/api/admin/access-points/openwrt-a/token", headers=auth_header("ADM001"))
    assert revoked.status_code == 200
    listed = client.get("/api/admin/access-points", headers=auth_header("ADM001"))
    assert listed.status_code == 200
    assert api_json(listed)["data"]["access_points"][0]["token_configured"] is False


def test_attendance_eligibility_persists_success_log() -> None:
    client, _ = make_client()
    response = client.post(
        "/api/attendance/eligibility",
        headers=auth_header("20201239"),
        json={"student_id": "20201239", "course_code": "CSE116"},
    )
    assert response.status_code == 200

    from app.db import get_db as backend_get_db
    from app.main import app as backend_app

    override = backend_app.dependency_overrides[backend_get_db]
    db = next(override())
    try:
        logs = db.scalars(select(PresenceEligibilityLog)).all()
        assert len(logs) == 1
        log = logs[0]
        assert log.purpose == "attendance"
        assert log.eligible is True
        assert log.reason_code == "OK"
        assert log.matched_device_mac == "52:54:00:12:34:56"
        assert log.snapshot_age_seconds == 2
        assert log.evidence["classroomId"] == "B101"
    finally:
        db.close()


def test_attendance_eligibility_persists_device_denial_log() -> None:
    client, _ = make_client()
    from app.db import get_db as backend_get_db
    from app.main import app as backend_app

    override = backend_app.dependency_overrides[backend_get_db]
    db = next(override())
    try:
        device = db.scalar(select(RegisteredDevice).where(RegisteredDevice.mac_address == "52:54:00:12:34:56"))
        db.delete(device)
        db.commit()
    finally:
        db.close()

    response = client.post(
        "/api/attendance/eligibility",
        headers=auth_header("20201239"),
        json={"student_id": "20201239", "course_code": "CSE116"},
    )
    assert response.status_code == 200
    assert api_json(response)["reason_code"] == "DEVICE_NOT_REGISTERED"

    db = next(override())
    try:
        log = db.scalar(select(PresenceEligibilityLog))
        assert log is not None
        assert log.eligible is False
        assert log.reason_code == "DEVICE_NOT_REGISTERED"
        assert log.evidence == {}
    finally:
        db.close()


def test_attendance_eligibility_persists_stale_snapshot_denial_log() -> None:
    client, fake_presence_client = make_client()
    fake_presence_client.reason_code = "SNAPSHOT_STALE"

    response = client.post(
        "/api/attendance/eligibility",
        headers=auth_header("20201239"),
        json={"student_id": "20201239", "course_code": "CSE116"},
    )
    assert response.status_code == 200
    assert api_json(response)["eligible"] is False
    assert api_json(response)["reason_code"] == "SNAPSHOT_STALE"

    from app.db import get_db as backend_get_db
    from app.main import app as backend_app

    override = backend_app.dependency_overrides[backend_get_db]
    db = next(override())
    try:
        log = db.scalar(select(PresenceEligibilityLog))
        assert log is not None
        assert log.eligible is False
        assert log.reason_code == "SNAPSHOT_STALE"
        assert log.matched_device_mac == "52:54:00:12:34:56"
        assert log.snapshot_age_seconds == 2
    finally:
        db.close()


def test_attendance_eligibility_persists_stale_snapshot_log() -> None:
    client, fake_presence = make_client()
    fake_presence.next_eligibility_response = {
        "eligible": False,
        "reasonCode": "STALE_SNAPSHOT",
        "matchedDeviceMac": "52:54:00:12:34:56",
        "observedAt": "2026-04-07T14:40:00+09:00",
        "snapshotAgeSeconds": 1500,
        "evidence": {"snapshotStale": True},
    }

    response = client.post(
        "/api/attendance/eligibility",
        headers=auth_header("20201239"),
        json={"student_id": "20201239", "course_code": "CSE116"},
    )

    assert response.status_code == 200
    assert api_json(response)["reason_code"] == "STALE_SNAPSHOT"

    from app.db import get_db as backend_get_db
    from app.main import app as backend_app

    override = backend_app.dependency_overrides[backend_get_db]
    db = next(override())
    try:
        log = db.scalar(select(PresenceEligibilityLog).where(PresenceEligibilityLog.reason_code == "STALE_SNAPSHOT"))
        assert log is not None
        assert log.eligible is False
        assert log.snapshot_age_seconds == 1500
        assert log.evidence["snapshotStale"] is True
    finally:
        db.close()


def test_exam_presence_eligibility_persists_log() -> None:
    client, _ = make_client()
    from app.services import check_attendance_eligibility
    from app.db import get_db as backend_get_db
    from app.main import app as backend_app, presence_client

    override = backend_app.dependency_overrides[backend_get_db]
    db = next(override())
    try:
        result = check_attendance_eligibility(
            db=db,
            presence_client=presence_client,
            student_id="20201239",
            course_id="CSE116",
            classroom_id=None,
            purpose="exam",
        )
        assert result["eligible"] is True
        log = db.scalar(select(PresenceEligibilityLog).where(PresenceEligibilityLog.purpose == "exam"))
        assert log is not None
        assert log.reason_code == "OK"
        assert log.evidence["mode"] == "registered-device-only"
    finally:
        db.close()
