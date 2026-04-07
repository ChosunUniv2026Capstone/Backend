from __future__ import annotations

from collections.abc import Generator

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import Session, sessionmaker

from app.db import get_db
from app.main import app
from app.models import Base, Classroom, ClassroomNetwork, Course, CourseEnrollment, CourseSchedule, RegisteredDevice, User

from datetime import datetime, timedelta


class FakePresenceClient:
    def __init__(self) -> None:
        self.last_overlay_payload = None
        self.last_eligibility_payload = None

    def get_admin_snapshot(self, *, classroom_code: str):
        assert classroom_code == "B101"
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
        return {
            "eligible": True,
            "reasonCode": "OK",
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


def test_admin_presence_snapshot_dropdown_includes_registered_union() -> None:
    client, _ = make_client()
    response = client.get("/api/admin/presence/classrooms/B101/snapshot", headers=auth_header("ADM001"))
    assert response.status_code == 200
    payload = response.json()
    option = next(item for item in payload["deviceOptions"] if item["macAddress"] == "52:54:00:12:34:56")
    assert option["studentLoginId"] == "20201239"
    assert option["deviceLabel"] == "Choi Phone"


def test_generic_attendance_eligibility_returns_outside_window_when_no_active_schedule() -> None:
    client, _ = make_client()
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
    assert response.json()["reason_code"] == "OUTSIDE_CLASS_WINDOW"


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
    assert response.json()["reason_code"] == "CLASSROOM_CONFLICT"


def auth_header(login_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer dev-token:{login_id}"}


def test_admin_routes_require_admin_role() -> None:
    client, _ = make_client()
    assert client.get("/api/admin/users").status_code == 401
    assert client.get("/api/admin/users", headers=auth_header("20201239")).status_code == 403
    response = client.get("/api/admin/users", headers=auth_header("ADM001"))
    assert response.status_code == 200
    assert len(response.json()) == 4


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
    payload = response.json()
    assert payload["classroomCode"] == "B101"
    assert payload["classroomNetworks"][0]["signal_threshold_dbm"] is None
    station = payload["aps"][0]["stations"][0]
    assert station["ownerLoginId"] == "20201239"
    assert station["ownerName"] == "Kim Student 06"
    assert station["deviceLabel"] == "Choi Phone"
    assert any(option["macAddress"] == "52:54:00:12:34:56" for option in payload["deviceOptions"])


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
    assert allowed.json()["eligible"] is True
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
    assert response.json()["signal_threshold_dbm"] == -55
