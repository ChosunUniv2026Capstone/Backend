from datetime import datetime
import re

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import Classroom, ClassroomNetwork, Course, CourseEnrollment, CourseSchedule, Notice, RegisteredDevice, User
from app.presence_client import PresenceClient
from app.schemas import DeviceCreate

MAC_PATTERN = re.compile(r"^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$")
MAX_DEVICES_PER_STUDENT = 5


def normalize_mac(mac_address: str) -> str:
    normalized = mac_address.strip().lower()
    if not MAC_PATTERN.match(normalized):
        raise HTTPException(status_code=400, detail="invalid MAC address format")
    return normalized


def list_devices(db: Session, student_id: str) -> list[RegisteredDevice]:
    user = _get_student_user(db, student_id)
    return list(
        db.scalars(
            select(RegisteredDevice)
            .where(RegisteredDevice.user_id == user.id)
            .order_by(RegisteredDevice.created_at.asc(), RegisteredDevice.id.asc())
        )
    )


def create_device(db: Session, student_id: str, payload: DeviceCreate) -> RegisteredDevice:
    user = _get_student_user(db, student_id)
    current_count = db.scalar(
        select(func.count()).select_from(RegisteredDevice).where(
            RegisteredDevice.user_id == user.id,
            RegisteredDevice.status == "active",
        )
    )
    if current_count and current_count >= MAX_DEVICES_PER_STUDENT:
        raise HTTPException(status_code=400, detail="device limit reached")

    mac_address = normalize_mac(payload.mac_address)
    existing = db.scalar(select(RegisteredDevice).where(RegisteredDevice.mac_address == mac_address))
    if existing:
        raise HTTPException(status_code=409, detail="MAC address already registered")

    device = RegisteredDevice(user_id=user.id, label=payload.label.strip(), mac_address=mac_address)
    db.add(device)
    db.commit()
    db.refresh(device)
    return device


def delete_device(db: Session, student_id: str, device_id: int) -> None:
    user = _get_student_user(db, student_id)
    device = db.scalar(
        select(RegisteredDevice).where(
            RegisteredDevice.id == device_id,
            RegisteredDevice.user_id == user.id,
        )
    )
    if not device:
        raise HTTPException(status_code=404, detail="device not found")

    device.status = "deleted"
    db.commit()


def _get_student_user(db: Session, student_id: str) -> User:
    user = db.scalar(
        select(User).where(
            User.student_id == student_id,
            User.role == "student",
        )
    )
    if not user:
        raise HTTPException(status_code=404, detail="student not found")
    return user


def authenticate_user(db: Session, login_id: str, password: str) -> User:
    user = db.scalar(
        select(User).where(
            or_(
                User.student_id == login_id,
                User.professor_id == login_id,
                User.admin_id == login_id,
            )
        )
    )
    if not user or user.password != password:
        raise HTTPException(status_code=401, detail="invalid credentials")
    return user


def get_user_login_id(user: User) -> str:
    return user.student_id or user.professor_id or user.admin_id or ""


def get_user_by_login_id(db: Session, login_id: str) -> User:
    user = db.scalar(
        select(User).where(
            or_(
                User.student_id == login_id,
                User.professor_id == login_id,
                User.admin_id == login_id,
            )
        )
    )
    if not user:
        raise HTTPException(status_code=404, detail="user not found")
    return user


def _is_enrolled(db: Session, student_id: str, course_id: str) -> bool:
    user = _get_student_user(db, student_id)
    return db.scalar(
        select(CourseEnrollment.id)
        .join(Course, Course.id == CourseEnrollment.course_id)
        .where(
            CourseEnrollment.student_user_id == user.id,
            Course.course_code == course_id,
            CourseEnrollment.status == "active",
        )
    ) is not None


def list_student_courses(db: Session, student_id: str) -> list[dict]:
    user = _get_student_user(db, student_id)
    rows = db.execute(
        select(
            Course.id,
            Course.course_code,
            Course.title,
            User.name,
            Classroom.classroom_code,
        )
        .join(CourseEnrollment, CourseEnrollment.course_id == Course.id)
        .join(User, User.id == Course.professor_user_id, isouter=True)
        .join(CourseSchedule, CourseSchedule.course_id == Course.id, isouter=True)
        .join(Classroom, Classroom.id == CourseSchedule.classroom_id, isouter=True)
        .where(
            CourseEnrollment.student_user_id == user.id,
            CourseEnrollment.status == "active",
        )
        .order_by(Course.course_code.asc())
    )
    return [
        {
            "id": row[0],
            "course_code": row[1],
            "title": row[2],
            "professor_name": row[3],
            "classroom_code": row[4],
        }
        for row in rows
    ]


def list_professor_courses(db: Session, professor_id: str) -> list[dict]:
    professor = db.scalar(select(User).where(User.professor_id == professor_id, User.role == "professor"))
    if not professor:
        raise HTTPException(status_code=404, detail="professor not found")
    rows = db.execute(
        select(Course.id, Course.course_code, Course.title, Classroom.classroom_code)
        .join(CourseSchedule, CourseSchedule.course_id == Course.id, isouter=True)
        .join(Classroom, Classroom.id == CourseSchedule.classroom_id, isouter=True)
        .where(Course.professor_user_id == professor.id)
        .order_by(Course.course_code.asc())
    )
    return [
        {
            "id": row[0],
            "course_code": row[1],
            "title": row[2],
            "professor_name": professor.name,
            "classroom_code": row[3],
        }
        for row in rows
    ]


def list_notices(db: Session, login_id: str) -> list[dict]:
    user = get_user_by_login_id(db, login_id)
    stmt = (
        select(Notice.id, Notice.title, Notice.body, Course.course_code, User.name, Notice.created_at)
        .join(User, User.id == Notice.author_user_id)
        .join(Course, Course.id == Notice.course_id, isouter=True)
        .order_by(Notice.created_at.desc())
    )
    if user.role == "student":
        stmt = stmt.join(CourseEnrollment, CourseEnrollment.course_id == Notice.course_id).where(
            CourseEnrollment.student_user_id == user.id,
            CourseEnrollment.status == "active",
        )
    elif user.role == "professor":
        stmt = stmt.where(Notice.author_user_id == user.id)
    else:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "FORBIDDEN",
                "message": "notice access is not available for this role",
                "details": {"role": user.role},
            },
        )

    rows = db.execute(stmt)
    return [
        {
            "id": row[0],
            "title": row[1],
            "body": row[2],
            "course_code": row[3],
            "author_name": row[4],
            "created_at": row[5],
        }
        for row in rows
    ]


def get_notice_detail(db: Session, login_id: str, notice_id: int) -> dict:
    notice = next((item for item in list_notices(db, login_id) if item["id"] == notice_id), None)
    if not notice:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "NOTICE_NOT_FOUND",
                "message": "notice not found",
                "details": {"notice_id": notice_id},
            },
        )
    return notice


def create_notice(db: Session, professor_id: str, title: str, body: str, course_code: str | None) -> Notice:
    professor = db.scalar(select(User).where(User.professor_id == professor_id, User.role == "professor"))
    if not professor:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "PROFESSOR_NOT_FOUND",
                "message": "professor not found",
                "details": {"professor_id": professor_id},
            },
        )

    course_id = None
    if course_code:
        course = db.scalar(
            select(Course).where(Course.course_code == course_code, Course.professor_user_id == professor.id)
        )
        if not course:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "COURSE_NOT_FOUND",
                    "message": "course not found",
                    "details": {"course_code": course_code},
                },
            )
        course_id = course.id

    notice = Notice(author_user_id=professor.id, course_id=course_id, title=title.strip(), body=body.strip())
    db.add(notice)
    db.commit()
    db.refresh(notice)
    return notice


def list_users(db: Session) -> list[dict]:
    rows = db.scalars(select(User).order_by(User.role.asc(), User.name.asc()))
    return [
        {
            "id": user.id,
            "role": user.role,
            "login_id": get_user_login_id(user),
            "name": user.name,
        }
        for user in rows
    ]


def list_classrooms(db: Session) -> list[dict]:
    rows = db.scalars(select(Classroom).order_by(Classroom.classroom_code.asc()))
    return [
        {
            "id": classroom.id,
            "classroom_code": classroom.classroom_code,
            "name": classroom.name,
            "building": getattr(classroom, "building", None),
            "floor_label": getattr(classroom, "floor_label", None),
        }
        for classroom in rows
    ]


def list_classroom_networks(db: Session) -> list[dict]:
    rows = db.execute(
        select(
            ClassroomNetwork.id,
            Classroom.classroom_code,
            ClassroomNetwork.ap_id,
            ClassroomNetwork.ssid,
            ClassroomNetwork.gateway_host,
            ClassroomNetwork.signal_threshold_dbm,
            ClassroomNetwork.collection_mode,
        )
        .join(Classroom, Classroom.id == ClassroomNetwork.classroom_id)
        .order_by(Classroom.classroom_code.asc(), ClassroomNetwork.ap_id.asc())
    )
    return [
        {
            "id": row[0],
            "classroom_code": row[1],
            "ap_id": row[2],
            "ssid": row[3],
            "gateway_host": row[4],
            "signal_threshold_dbm": row[5],
            "collection_mode": row[6],
        }
        for row in rows
    ]


def list_classroom_networks_for_classroom(db: Session, classroom_code: str) -> list[dict]:
    return [
        network
        for network in list_classroom_networks(db)
        if network["classroom_code"] == classroom_code
    ]


def resolve_active_classroom_for_course(db: Session, course_id: str) -> str:
    now = datetime.now()
    weekday = now.weekday()
    current_time = now.time()
    rows = db.execute(
        select(Classroom.classroom_code)
        .select_from(CourseSchedule)
        .join(Course, Course.id == CourseSchedule.course_id)
        .join(Classroom, Classroom.id == CourseSchedule.classroom_id)
        .where(
            Course.course_code == course_id,
            CourseSchedule.day_of_week == weekday,
            CourseSchedule.starts_at <= current_time,
            CourseSchedule.ends_at >= current_time,
        )
    )
    classroom_codes = {row[0] for row in rows}
    if not classroom_codes:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "OUTSIDE_CLASS_WINDOW",
                "message": "no active classroom mapping for the current course window",
                "details": {"course_code": course_id},
            },
        )
    if len(classroom_codes) == 1:
        return classroom_codes.pop()
    raise HTTPException(
        status_code=409,
        detail={
            "code": "CLASSROOM_CONFLICT",
            "message": "multiple active classrooms were resolved for the current course window",
            "details": {"course_code": course_id, "classroom_codes": sorted(classroom_codes)},
        },
    )


def list_presence_device_options(db: Session, classroom_code: str) -> list[dict]:
    now = datetime.now()
    weekday = now.weekday()
    current_time = now.time()
    observed_rows = db.execute(
        select(
            RegisteredDevice.mac_address,
            RegisteredDevice.label,
            User.student_id,
            User.name,
        )
        .join(User, User.id == RegisteredDevice.user_id)
        .join(CourseEnrollment, CourseEnrollment.student_user_id == User.id)
        .join(CourseSchedule, CourseSchedule.course_id == CourseEnrollment.course_id)
        .join(Classroom, Classroom.id == CourseSchedule.classroom_id)
        .where(
            User.role == "student",
            CourseEnrollment.status == "active",
            Classroom.classroom_code == classroom_code,
            CourseSchedule.day_of_week == weekday,
            CourseSchedule.starts_at <= current_time,
            CourseSchedule.ends_at >= current_time,
        )
    )

    device_index: dict[str, dict] = {}
    for mac_address, device_label, student_id, student_name in observed_rows:
        device_index[mac_address.lower()] = {
            "student_login_id": student_id,
            "student_name": student_name,
            "device_label": device_label,
            "mac_address": mac_address.lower(),
            "observed": False,
        }
    return sorted(device_index.values(), key=lambda item: (item["student_login_id"], item["device_label"], item["mac_address"]))


def update_classroom_network_threshold(db: Session, network_id: int, signal_threshold_dbm: int | None) -> dict:
    network = db.scalar(select(ClassroomNetwork).where(ClassroomNetwork.id == network_id))
    if network is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "CLASSROOM_NETWORK_NOT_FOUND",
                "message": "classroom network not found",
                "details": {"network_id": network_id},
            },
        )
    network.signal_threshold_dbm = signal_threshold_dbm
    db.commit()
    db.refresh(network)
    classroom = db.scalar(select(Classroom).where(Classroom.id == network.classroom_id))
    return {
        "id": network.id,
        "classroom_code": classroom.classroom_code if classroom else "",
        "ap_id": network.ap_id,
        "ssid": network.ssid,
        "gateway_host": network.gateway_host,
        "signal_threshold_dbm": network.signal_threshold_dbm,
        "collection_mode": network.collection_mode,
    }


def check_attendance_eligibility(
    *,
    db: Session,
    presence_client: PresenceClient,
    student_id: str,
    course_id: str,
    classroom_id: str | None,
    purpose: str = "attendance",
) -> dict:
    if not _is_enrolled(db, student_id, course_id):
        raise HTTPException(status_code=403, detail="student is not enrolled in the course")

    try:
        resolved_classroom_id = resolve_active_classroom_for_course(db, course_id)
    except HTTPException as exc:
        return {
            "eligible": False,
            "reason_code": exc.detail["code"] if isinstance(exc.detail, dict) else "OUTSIDE_CLASS_WINDOW",
            "matched_device_mac": None,
            "observed_at": None,
            "snapshot_age_seconds": None,
            "evidence": exc.detail.get("details", {}) if isinstance(exc.detail, dict) else {},
        }

    devices = [device for device in list_devices(db, student_id) if device.status == "active"]
    registered_devices = [{"mac": device.mac_address, "label": device.label} for device in devices]
    if not registered_devices:
        return {
            "eligible": False,
            "reason_code": "DEVICE_NOT_REGISTERED",
            "matched_device_mac": None,
            "observed_at": None,
            "snapshot_age_seconds": None,
            "evidence": {},
        }

    presence_payload = presence_client.check_eligibility(
        student_id=student_id,
        course_id=course_id,
        classroom_id=resolved_classroom_id,
        purpose=purpose,
        classroom_networks=[
            {
                "apId": network["ap_id"],
                "ssid": network["ssid"],
                "signalThresholdDbm": network["signal_threshold_dbm"],
            }
            for network in list_classroom_networks_for_classroom(db, resolved_classroom_id)
        ],
        registered_devices=registered_devices,
    )

    return {
        "eligible": bool(presence_payload.get("eligible")),
        "reason_code": presence_payload.get("reasonCode", "UNKNOWN"),
        "matched_device_mac": presence_payload.get("matchedDeviceMac"),
        "observed_at": presence_payload.get("observedAt"),
        "snapshot_age_seconds": presence_payload.get("snapshotAgeSeconds"),
        "evidence": presence_payload.get("evidence", {}),
    }
