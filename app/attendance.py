from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal

from fastapi import HTTPException
from sqlalchemy import Select, desc, func, or_, select
from sqlalchemy.orm import Session

from app.models import (
    AttendanceRecord,
    AttendanceSession,
    AttendanceSessionSlot,
    AttendanceStatusAuditLog,
    Classroom,
    ClassroomNetwork,
    Course,
    CourseEnrollment,
    CourseSchedule,
    RegisteredDevice,
    User,
)
from app.presence_client import PresenceClient

SEMESTER_START = date(2026, 3, 3)
SEMESTER_END = date(2026, 6, 30)
SMART_ATTENDANCE_WINDOW_MINUTES = 10
FINAL_STATUSES = {"present", "absent", "late", "official", "sick"}
SESSION_MODES = {"manual", "smart", "canceled"}
WEEKDAY_LABELS = ["월", "화", "수", "목", "금", "토", "일"]


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@dataclass(frozen=True)
class ProjectionSlot:
    projection_key: str
    course_code: str
    classroom_code: str
    session_date: date
    slot_start_at: time
    slot_end_at: time
    week_index: int
    lesson_index_within_week: int
    period_index_within_day: int
    period_label: str
    display_label: str
    professor_name: str
    professor_login_id: str


@dataclass(frozen=True)
class SessionSlotAssignment:
    attendance_session_id: int
    projection_key: str
    classroom_id: int
    session_date: date
    slot_start_at: time
    slot_end_at: time
    slot_order: int


def attendance_api_error(status_code: int, code: str, message: str, details: dict[str, Any] | None = None) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={
            "code": code,
            "message": message,
            "details": details or {},
        },
    )


def get_professor_user(db: Session, professor_id: str) -> User:
    professor = db.scalar(select(User).where(User.professor_id == professor_id, User.role == "professor"))
    if professor is None:
        raise attendance_api_error(404, "PROFESSOR_NOT_FOUND", "professor not found", {"professor_id": professor_id})
    return professor


def get_student_user(db: Session, student_id: str) -> User:
    student = db.scalar(select(User).where(User.student_id == student_id, User.role == "student"))
    if student is None:
        raise attendance_api_error(404, "STUDENT_NOT_FOUND", "student not found", {"student_id": student_id})
    return student


def get_course_by_code(db: Session, course_code: str) -> Course:
    course = db.scalar(select(Course).where(Course.course_code == course_code))
    if course is None:
        raise attendance_api_error(404, "COURSE_NOT_FOUND", "course not found", {"course_code": course_code})
    return course


def get_owned_course(db: Session, professor_id: str, course_code: str) -> tuple[User, Course]:
    professor = get_professor_user(db, professor_id)
    course = db.scalar(
        select(Course).where(Course.course_code == course_code, Course.professor_user_id == professor.id)
    )
    if course is None:
        raise attendance_api_error(
            403,
            "FORBIDDEN",
            "course does not belong to the authenticated professor",
            {"professor_id": professor_id, "course_code": course_code},
        )
    return professor, course


def ensure_student_enrolled(db: Session, student_user_id: int, course_id: int, student_login_id: str, course_code: str) -> None:
    enrolled = db.scalar(
        select(CourseEnrollment.id).where(
            CourseEnrollment.course_id == course_id,
            CourseEnrollment.student_user_id == student_user_id,
            CourseEnrollment.status == "active",
        )
    )
    if enrolled is None:
        raise attendance_api_error(
            403,
            "FORBIDDEN",
            "student is not enrolled in the course",
            {"student_id": student_login_id, "course_code": course_code},
        )


def _semester_anchor_start() -> date:
    return SEMESTER_START - timedelta(days=SEMESTER_START.weekday())


def create_projection_key(course_code: str, classroom_code: str, session_date: date, slot_start_at: time, slot_end_at: time) -> str:
    return f"{course_code}:{classroom_code}:{session_date.isoformat()}:{slot_start_at.isoformat()}:{slot_end_at.isoformat()}"


def _format_display_label(lesson_index_within_week: int, period_index_within_day: int, session_date: date, professor_name: str, professor_login_id: str) -> str:
    return (
        f"{lesson_index_within_week}차시({period_index_within_day}교시): "
        f"{session_date.strftime('%Y.%m.%d')}({WEEKDAY_LABELS[session_date.weekday()]}) "
        f"{professor_name}({professor_login_id})"
    )


def _projection_slot_rows(db: Session, course: Course, professor: User) -> list[ProjectionSlot]:
    rows = db.execute(
        select(CourseSchedule.day_of_week, CourseSchedule.starts_at, CourseSchedule.ends_at, Classroom.classroom_code)
        .join(Classroom, Classroom.id == CourseSchedule.classroom_id)
        .where(CourseSchedule.course_id == course.id)
        .order_by(CourseSchedule.day_of_week.asc(), CourseSchedule.starts_at.asc())
    ).all()

    raw_slots: list[dict[str, Any]] = []
    cursor = SEMESTER_START
    while cursor <= SEMESTER_END:
        for day_of_week, starts_at, ends_at, classroom_code in rows:
            if cursor.weekday() != day_of_week:
                continue

            start_dt = datetime.combine(cursor, starts_at)
            end_dt = datetime.combine(cursor, ends_at)
            period_index = 1
            while start_dt + timedelta(minutes=30) <= end_dt:
                slot_end_dt = start_dt + timedelta(minutes=30)
                projection_key = create_projection_key(course.course_code, classroom_code, cursor, start_dt.time(), slot_end_dt.time())
                raw_slots.append(
                    {
                        "projection_key": projection_key,
                        "course_code": course.course_code,
                        "classroom_code": classroom_code,
                        "session_date": cursor,
                        "slot_start_at": start_dt.time().replace(microsecond=0),
                        "slot_end_at": slot_end_dt.time().replace(microsecond=0),
                        "week_start": cursor - timedelta(days=cursor.weekday()),
                        "period_index_within_day": period_index,
                    }
                )
                start_dt = slot_end_dt
                period_index += 1
        cursor += timedelta(days=1)

    raw_slots.sort(key=lambda item: (item["session_date"], item["slot_start_at"], item["classroom_code"]))
    week_counters: dict[date, int] = defaultdict(int)
    semester_anchor = _semester_anchor_start()
    slots: list[ProjectionSlot] = []
    for item in raw_slots:
        week_start = item["week_start"]
        week_counters[week_start] += 1
        week_index = ((week_start - semester_anchor).days // 7) + 1
        lesson_index_within_week = week_counters[week_start]
        professor_login_id = professor.professor_id or ""
        slots.append(
            ProjectionSlot(
                projection_key=item["projection_key"],
                course_code=item["course_code"],
                classroom_code=item["classroom_code"],
                session_date=item["session_date"],
                slot_start_at=item["slot_start_at"],
                slot_end_at=item["slot_end_at"],
                week_index=week_index,
                lesson_index_within_week=lesson_index_within_week,
                period_index_within_day=item["period_index_within_day"],
                period_label=f"{item['period_index_within_day']}교시",
                display_label=_format_display_label(
                    lesson_index_within_week,
                    item["period_index_within_day"],
                    item["session_date"],
                    professor.name,
                    professor_login_id,
                ),
                professor_name=professor.name,
                professor_login_id=professor_login_id,
            )
        )
    return slots


def _serialize_dt(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC).isoformat()
    return value.astimezone(UTC).isoformat()


def _coerce_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _serialize_date(value: date) -> str:
    return value.isoformat()


def _serialize_time(value: time) -> str:
    return value.replace(microsecond=0).isoformat()


def _fallback_session_assignment(session: AttendanceSession) -> SessionSlotAssignment:
    return SessionSlotAssignment(
        attendance_session_id=session.id,
        projection_key=session.projection_key,
        classroom_id=session.classroom_id,
        session_date=session.session_date,
        slot_start_at=session.slot_start_at,
        slot_end_at=session.slot_end_at,
        slot_order=0,
    )


def _session_slot_assignments(db: Session, sessions: list[AttendanceSession]) -> dict[int, list[SessionSlotAssignment]]:
    if not sessions:
        return {}

    rows = db.scalars(
        select(AttendanceSessionSlot)
        .where(AttendanceSessionSlot.attendance_session_id.in_([session.id for session in sessions]))
        .order_by(AttendanceSessionSlot.slot_order.asc(), AttendanceSessionSlot.id.asc())
    ).all()

    assignments: dict[int, list[SessionSlotAssignment]] = defaultdict(list)
    for row in rows:
        assignments[row.attendance_session_id].append(
            SessionSlotAssignment(
                attendance_session_id=row.attendance_session_id,
                projection_key=row.projection_key,
                classroom_id=row.classroom_id,
                session_date=row.session_date,
                slot_start_at=row.slot_start_at,
                slot_end_at=row.slot_end_at,
                slot_order=row.slot_order,
            )
        )

    for session in sessions:
        assignments.setdefault(session.id, [_fallback_session_assignment(session)])
    return assignments


def _session_assignments_for_one(db: Session, session: AttendanceSession) -> list[SessionSlotAssignment]:
    return _session_slot_assignments(db, [session]).get(session.id, [_fallback_session_assignment(session)])


def _serialize_assignment(slot_map: dict[str, ProjectionSlot], assignment: SessionSlotAssignment) -> dict[str, Any]:
    slot = slot_map.get(assignment.projection_key)
    return {
        "projection_key": assignment.projection_key,
        "classroom_code": slot.classroom_code if slot else None,
        "session_date": _serialize_date(assignment.session_date),
        "slot_start_at": _serialize_time(assignment.slot_start_at),
        "slot_end_at": _serialize_time(assignment.slot_end_at),
        "display_label": slot.display_label if slot else assignment.projection_key,
        "period_label": slot.period_label if slot else None,
        "slot_order": assignment.slot_order,
    }


def _assignment_from_projection_slot(classroom_id: int, slot: ProjectionSlot, *, slot_order: int = 0) -> SessionSlotAssignment:
    return SessionSlotAssignment(
        attendance_session_id=0,
        projection_key=slot.projection_key,
        classroom_id=classroom_id,
        session_date=slot.session_date,
        slot_start_at=slot.slot_start_at,
        slot_end_at=slot.slot_end_at,
        slot_order=slot_order,
    )


def _bundle_bounds(assignments: list[SessionSlotAssignment]) -> tuple[time, time]:
    ordered = sorted(assignments, key=lambda item: (item.slot_start_at, item.slot_order))
    return ordered[0].slot_start_at, max(item.slot_end_at for item in ordered)


def _projection_lookup_by_session(
    db: Session,
    course_id: int,
) -> tuple[dict[str, AttendanceSession], dict[int, list[SessionSlotAssignment]]]:
    sessions = db.scalars(
        select(AttendanceSession)
        .where(AttendanceSession.course_id == course_id)
        .order_by(desc(AttendanceSession.opened_at), desc(AttendanceSession.id))
    ).all()
    assignments_by_session = _session_slot_assignments(db, sessions)
    latest: dict[str, AttendanceSession] = {}
    for session in sessions:
        for assignment in assignments_by_session.get(session.id, [_fallback_session_assignment(session)]):
            latest.setdefault(assignment.projection_key, session)
    return latest, assignments_by_session


def _latest_session_for_projection(db: Session, projection_key: str) -> AttendanceSession | None:
    sessions = db.scalars(
        select(AttendanceSession)
        .outerjoin(
            AttendanceSessionSlot,
            AttendanceSessionSlot.attendance_session_id == AttendanceSession.id,
        )
        .where(
            (AttendanceSession.projection_key == projection_key)
            | (AttendanceSessionSlot.projection_key == projection_key)
        )
        .order_by(desc(AttendanceSession.opened_at), desc(AttendanceSession.id))
    ).all()
    return sessions[0] if sessions else None


def _records_for_sessions(
    db: Session,
    session_ids: list[int],
) -> dict[tuple[int, str, int], AttendanceRecord]:
    if not session_ids:
        return {}
    records = db.scalars(select(AttendanceRecord).where(AttendanceRecord.attendance_session_id.in_(session_ids))).all()
    return {
        (record.attendance_session_id, record.projection_key, record.student_user_id): record
        for record in records
    }


def _resolved_slot_status(
    session: AttendanceSession,
    projection_key: str,
    student_user_id: int,
    record_lookup: dict[tuple[int, str, int], AttendanceRecord],
) -> str | None:
    record = record_lookup.get((session.id, projection_key, student_user_id))
    if record is not None:
        return record.final_status
    if session.mode == "canceled" or session.status == "canceled":
        return None
    if session.mode == "smart" and session.status == "active":
        return None
    return "absent"


def _resolved_counts_for_session_slots(
    session: AttendanceSession,
    assignments: list[SessionSlotAssignment],
    enrolled_user_ids: list[int],
    record_lookup: dict[tuple[int, str, int], AttendanceRecord],
) -> dict[str, dict[str, int]]:
    counts_by_projection = {
        assignment.projection_key: _counts_template()
        for assignment in assignments
    }
    for assignment in assignments:
        bucket = counts_by_projection[assignment.projection_key]
        for student_user_id in enrolled_user_ids:
            status = _resolved_slot_status(session, assignment.projection_key, student_user_id, record_lookup)
            if status is None:
                continue
            bucket[status] = bucket.get(status, 0) + 1
    return counts_by_projection


def _aggregate_projection_counts(
    counts_by_projection: dict[str, dict[str, int]],
    assignments: list[SessionSlotAssignment],
) -> dict[str, int]:
    aggregate = _counts_template()
    for assignment in assignments:
        counts = counts_by_projection.get(assignment.projection_key, _counts_template())
        for key, value in counts.items():
            aggregate[key] += value
    return aggregate


def _resolved_counts_by_session_for_course(
    db: Session,
    course_id: int,
    latest_sessions: dict[str, AttendanceSession],
    assignments_by_session: dict[int, list[SessionSlotAssignment]],
) -> tuple[dict[int, dict[str, dict[str, int]]], dict[tuple[int, str, int], AttendanceRecord], list[tuple[int, str, str]]]:
    enrolled_rows = _enrolled_students_for_course(db, course_id)
    enrolled_user_ids = [student_user_id for student_user_id, _, _ in enrolled_rows]
    unique_sessions = {session.id: session for session in latest_sessions.values()}
    record_lookup = _records_for_sessions(db, list(unique_sessions))
    counts_by_session: dict[int, dict[str, dict[str, int]]] = {}
    for session in unique_sessions.values():
        assignments = assignments_by_session.get(session.id, [_fallback_session_assignment(session)])
        counts_by_session[session.id] = _resolved_counts_for_session_slots(
            session,
            assignments,
            enrolled_user_ids,
            record_lookup,
        )
    return counts_by_session, record_lookup, enrolled_rows


def _materialize_smart_session_absences(
    db: Session,
    session: AttendanceSession,
    *,
    actor_user_id: int,
    actor_role: str,
    change_source: str,
    version: int,
    changed_at: datetime,
) -> None:
    if session.mode != "smart":
        return
    assignments = _session_assignments_for_one(db, session)
    enrolled_rows = _enrolled_students_for_course(db, session.course_id)
    existing = _records_for_session(db, session.id)
    for student_user_id, _, _ in enrolled_rows:
        for assignment in assignments:
            key = (student_user_id, assignment.projection_key)
            if key in existing:
                continue
            record = AttendanceRecord(
                attendance_session_id=session.id,
                projection_key=assignment.projection_key,
                student_user_id=student_user_id,
                final_status="absent",
                attendance_reason=None,
                finalized_by_user_id=actor_user_id,
                finalized_at=changed_at,
            )
            existing[key] = record
            db.add(record)
            db.add(
                AttendanceStatusAuditLog(
                    attendance_session_id=session.id,
                    projection_key=assignment.projection_key,
                    student_user_id=student_user_id,
                    actor_user_id=actor_user_id,
                    actor_role=actor_role,
                    change_source=change_source,
                    previous_status=None,
                    new_status="absent",
                    reason="미체크 자동 결석 확정",
                    changed_at=changed_at,
                    version=version,
                )
            )


def expire_stale_attendance_sessions(db: Session, course_code: str | None = None) -> list[dict[str, Any]]:
    query: Select[tuple[AttendanceSession, Course]] = (
        select(AttendanceSession, Course)
        .join(Course, Course.id == AttendanceSession.course_id)
        .where(
            AttendanceSession.mode == "smart",
            AttendanceSession.status == "active",
            AttendanceSession.expires_at.is_not(None),
        )
    )
    if course_code is not None:
        query = query.where(Course.course_code == course_code)

    expired_events: list[dict[str, Any]] = []
    now = datetime.now(UTC)
    rows = db.execute(query).all()
    assignments_by_session = _session_slot_assignments(db, [session for session, _ in rows])
    for session, course in rows:
        expires_at = _coerce_utc(session.expires_at)
        if expires_at is None or expires_at > now:
            continue
        next_version = session.latest_version + 1
        _materialize_smart_session_absences(
            db,
            session,
            actor_user_id=course.professor_user_id,
            actor_role="professor",
            change_source="smart-expire-default",
            version=next_version,
            changed_at=now,
        )
        session.status = "expired"
        session.closed_at = now.replace(tzinfo=None)
        session.expires_at = None
        session.latest_version = next_version
        projection_keys = [assignment.projection_key for assignment in assignments_by_session.get(session.id, [])]
        expired_events.append(
            {
                "course_code": course.course_code,
                "projection_key": session.projection_key,
                "projection_keys": projection_keys or [session.projection_key],
                "session_id": session.id,
                "version": session.latest_version,
                "event_type": "session.expired",
                "occurred_at": _serialize_dt(now),
            }
        )
    if expired_events:
        db.commit()
    return expired_events


def _slot_state(session: AttendanceSession | None) -> str:
    if session is None:
        return "unchecked"
    if session.mode == "canceled" or session.status == "canceled":
        return "canceled"
    if session.mode == "smart":
        return "online"
    return "offline"



def _counts_template() -> dict[str, int]:
    return {"present": 0, "late": 0, "absent": 0, "official": 0, "sick": 0}



def _projection_keys_for_session(
    assignments_by_session: dict[int, list[SessionSlotAssignment]],
    session: AttendanceSession | None,
) -> list[str]:
    if session is None:
        return []
    return [assignment.projection_key for assignment in assignments_by_session.get(session.id, [_fallback_session_assignment(session)])]



def _serialize_slot(
    slot: ProjectionSlot,
    session: AttendanceSession | None,
    counts: dict[str, int],
    session_projection_keys: list[str],
) -> dict[str, Any]:
    official_total = counts.get("official", 0) + counts.get("sick", 0)
    return {
        "projection_key": slot.projection_key,
        "course_code": slot.course_code,
        "classroom_code": slot.classroom_code,
        "session_date": _serialize_date(slot.session_date),
        "slot_start_at": _serialize_time(slot.slot_start_at),
        "slot_end_at": _serialize_time(slot.slot_end_at),
        "week_index": slot.week_index,
        "lesson_index_within_week": slot.lesson_index_within_week,
        "period_label": slot.period_label,
        "display_label": slot.display_label,
        "professor_name": slot.professor_name,
        "professor_login_id": slot.professor_login_id,
        "slot_state": _slot_state(session),
        "session_id": session.id if session else None,
        "session_mode": session.mode if session else None,
        "session_status": session.status if session else None,
        "expires_at": (
            _serialize_dt(session.expires_at)
            if session and session.mode == "smart" and session.status == "active"
            else None
        ),
        "bundle_projection_keys": session_projection_keys,
        "bundle_slot_count": len(session_projection_keys),
        "is_anchor_slot": bool(session and session.projection_key == slot.projection_key),
        "aggregate": {
            "present": counts.get("present", 0),
            "late": counts.get("late", 0),
            "absent": counts.get("absent", 0),
            "official": official_total,
            "sick": counts.get("sick", 0),
        },
    }



def build_attendance_timeline(db: Session, professor_id: str, course_code: str) -> dict[str, Any]:
    professor, course = get_owned_course(db, professor_id, course_code)
    expire_stale_attendance_sessions(db, course_code)
    slots = _projection_slot_rows(db, course, professor)
    latest_sessions, assignments_by_session = _projection_lookup_by_session(db, course.id)
    counts_by_session, _, _ = _resolved_counts_by_session_for_course(
        db,
        course.id,
        latest_sessions,
        assignments_by_session,
    )

    weeks: dict[int, dict[str, Any]] = {}
    for slot in slots:
        session = latest_sessions.get(slot.projection_key)
        session_projection_keys = _projection_keys_for_session(assignments_by_session, session)
        counts = (
            counts_by_session.get(session.id, {}).get(slot.projection_key, _counts_template())
            if session
            else _counts_template()
        )
        serialized = _serialize_slot(slot, session, counts, session_projection_keys)
        week_start = slot.session_date - timedelta(days=slot.session_date.weekday())
        bucket = weeks.setdefault(
            slot.week_index,
            {
                "week_index": slot.week_index,
                "week_start": _serialize_date(week_start),
                "week_end": _serialize_date(week_start + timedelta(days=6)),
                "slots": [],
            },
        )
        bucket["slots"].append(serialized)

    report_summary = build_attendance_report(db, professor_id, course_code)
    return {
        "course_code": course.course_code,
        "course_title": course.title,
        "semester_start": _serialize_date(SEMESTER_START),
        "semester_end": _serialize_date(SEMESTER_END),
        "weeks": [weeks[index] for index in sorted(weeks)],
        "report_summary": report_summary,
    }



def build_attendance_report(db: Session, professor_id: str, course_code: str) -> dict[str, Any]:
    professor, course = get_owned_course(db, professor_id, course_code)
    expire_stale_attendance_sessions(db, course_code)
    slots = _projection_slot_rows(db, course, professor)
    latest_sessions, _ = _projection_lookup_by_session(db, course.id)
    assignments_by_session = _session_slot_assignments(db, list({session.id: session for session in latest_sessions.values()}.values()))
    counts_by_session, _, _ = _resolved_counts_by_session_for_course(
        db,
        course.id,
        latest_sessions,
        assignments_by_session,
    )

    summary = {
        "projection_slot_count": len(slots),
        "active_session_count": 0,
        "smart_active_count": 0,
        "canceled_count": 0,
        "present": 0,
        "late": 0,
        "absent": 0,
        "official": 0,
        "sick": 0,
    }
    active_session_ids: set[int] = set()
    smart_session_ids: set[int] = set()
    canceled_session_ids: set[int] = set()
    for slot in slots:
        session = latest_sessions.get(slot.projection_key)
        if session is None:
            continue
        counts = counts_by_session.get(session.id, {}).get(slot.projection_key, _counts_template())
        if session.status == "active":
            active_session_ids.add(session.id)
            if session.mode == "smart":
                smart_session_ids.add(session.id)
        if session.mode == "canceled" or session.status == "canceled":
            canceled_session_ids.add(session.id)
        for key in ("present", "late", "absent", "official", "sick"):
            summary[key] += counts.get(key, 0)
    summary["active_session_count"] = len(active_session_ids)
    summary["smart_active_count"] = len(smart_session_ids)
    summary["canceled_count"] = len(canceled_session_ids)
    return summary



def _projection_slot_lookup(db: Session, course: Course, professor: User) -> dict[str, ProjectionSlot]:
    return {slot.projection_key: slot for slot in _projection_slot_rows(db, course, professor)}



def open_attendance_sessions_batch(
    db: Session,
    professor_id: str,
    course_code: str,
    *,
    projection_keys: list[str],
    mode: Literal["manual", "smart", "canceled"],
) -> dict[str, Any]:
    if mode not in SESSION_MODES:
        raise attendance_api_error(400, "INVALID_SESSION_MODE", "invalid attendance session mode", {"mode": mode})
    professor, course = get_owned_course(db, professor_id, course_code)
    slot_map = _projection_slot_lookup(db, course, professor)
    now = _utcnow()
    deduped_projection_keys = list(dict.fromkeys(projection_keys))
    results: list[dict[str, Any]] = []
    valid_assignments: list[SessionSlotAssignment] = []
    target_session_date: date | None = None
    previous_sessions: dict[str, AttendanceSession | None] = {}

    for order, projection_key in enumerate(deduped_projection_keys):
        slot = slot_map.get(projection_key)
        if slot is None:
            results.append(
                {
                    "projection_key": projection_key,
                    "success": False,
                    "code": "SESSION_SLOT_INVALID",
                    "message": "projection key is not a valid slot for the course",
                    "session_id": None,
                    "resulting_slot_state": "unchecked",
                }
            )
            continue
        if target_session_date is None:
            target_session_date = slot.session_date
        elif slot.session_date != target_session_date:
            results.append(
                {
                    "projection_key": projection_key,
                    "success": False,
                    "code": "SESSION_SLOT_INVALID",
                    "message": "batch attendance operations must stay within the same date",
                    "session_id": None,
                    "resulting_slot_state": "unchecked",
                }
            )
            continue

        active_existing = db.scalar(
            select(AttendanceSession)
            .outerjoin(AttendanceSessionSlot, AttendanceSessionSlot.attendance_session_id == AttendanceSession.id)
            .where(
                AttendanceSession.status == "active",
                or_(
                    AttendanceSession.projection_key == projection_key,
                    AttendanceSessionSlot.projection_key == projection_key,
                ),
            )
            .order_by(desc(AttendanceSession.opened_at), desc(AttendanceSession.id))
        )
        if active_existing is not None:
            results.append(
                {
                    "projection_key": projection_key,
                    "success": False,
                    "code": "SESSION_ALREADY_OPEN",
                    "message": "an active session already exists for the projection key",
                    "session_id": active_existing.id,
                    "resulting_slot_state": _slot_state(active_existing),
                }
            )
            continue

        classroom_id = db.scalar(select(Classroom.id).where(Classroom.classroom_code == slot.classroom_code))
        if classroom_id is None:
            results.append(
                {
                    "projection_key": projection_key,
                    "success": False,
                    "code": "SESSION_SLOT_INVALID",
                    "message": "projection slot classroom is missing",
                    "session_id": None,
                    "resulting_slot_state": "unchecked",
                }
            )
            continue

        valid_assignments.append(
            SessionSlotAssignment(
                attendance_session_id=0,
                projection_key=projection_key,
                classroom_id=classroom_id,
                session_date=slot.session_date,
                slot_start_at=slot.slot_start_at,
                slot_end_at=slot.slot_end_at,
                slot_order=order,
            )
        )
        previous_sessions[projection_key] = _latest_session_for_projection(db, projection_key)

    if valid_assignments:
        anchor = valid_assignments[0]
        bundle_start_at, bundle_end_at = _bundle_bounds(valid_assignments)
        session = AttendanceSession(
            projection_key=anchor.projection_key,
            course_id=course.id,
            classroom_id=anchor.classroom_id,
            session_date=anchor.session_date,
            slot_start_at=bundle_start_at,
            slot_end_at=bundle_end_at,
            mode=mode,
            status="canceled" if mode == "canceled" else "active",
            opened_by_user_id=professor.id,
            opened_at=now,
            closed_at=now if mode == "canceled" else None,
            expires_at=now + timedelta(minutes=SMART_ATTENDANCE_WINDOW_MINUTES) if mode == "smart" else None,
            latest_version=1,
        )
        db.add(session)
        db.flush()
        for assignment in valid_assignments:
            db.add(
                AttendanceSessionSlot(
                    attendance_session_id=session.id,
                    projection_key=assignment.projection_key,
                    classroom_id=assignment.classroom_id,
                    session_date=assignment.session_date,
                    slot_start_at=assignment.slot_start_at,
                    slot_end_at=assignment.slot_end_at,
                    slot_order=assignment.slot_order,
                )
            )

        success_rows = {
            assignment.projection_key: {
                "projection_key": assignment.projection_key,
                "success": True,
                "code": "OK",
                "message": "attendance session applied",
                "session_id": session.id,
                "resulting_slot_state": _slot_state(session),
                "event_type": (
                    "session.canceled"
                    if mode == "canceled"
                    else "session.reopened"
                    if previous_sessions.get(assignment.projection_key) is not None
                    else "session.opened"
                ),
                "expires_at": _serialize_dt(session.expires_at),
            }
            for assignment in valid_assignments
        }
        final_results: list[dict[str, Any]] = []
        for projection_key in deduped_projection_keys:
            matching = next((row for row in results if row["projection_key"] == projection_key), None)
            final_results.append(success_rows.get(projection_key, matching))
        results = [row for row in final_results if row is not None]
        db.commit()
        return {
            "course_code": course.course_code,
            "mode": mode,
            "results": results,
            "changed_projection_keys": [assignment.projection_key for assignment in valid_assignments],
            "changed_session_ids": [session.id],
            "occurred_at": _serialize_dt(now),
        }

    return {
        "course_code": course.course_code,
        "mode": mode,
        "results": results,
        "changed_projection_keys": [],
        "changed_session_ids": [],
        "occurred_at": _serialize_dt(now),
    }



def close_attendance_session(db: Session, professor_id: str, session_id: int) -> dict[str, Any]:
    professor = get_professor_user(db, professor_id)
    session = db.scalar(select(AttendanceSession).where(AttendanceSession.id == session_id))
    if session is None:
        raise attendance_api_error(404, "ATTENDANCE_SESSION_NOT_FOUND", "attendance session not found", {"session_id": session_id})
    course = db.scalar(select(Course).where(Course.id == session.course_id))
    if course is None or course.professor_user_id != professor.id:
        raise attendance_api_error(403, "FORBIDDEN", "session does not belong to the authenticated professor")
    projection_keys = _projection_keys_for_session({session.id: _session_assignments_for_one(db, session)}, session)
    if session.status != "active":
        return {
            "session_id": session.id,
            "projection_key": session.projection_key,
            "projection_keys": projection_keys,
            "status": session.status,
            "version": session.latest_version,
            "occurred_at": _serialize_dt(_utcnow()),
        }
    expires_at = _coerce_utc(session.expires_at)
    now = _utcnow()
    next_status = "expired" if session.mode == "smart" and expires_at and expires_at <= datetime.now(UTC) else "closed"
    next_version = session.latest_version + 1
    if session.mode == "smart":
        _materialize_smart_session_absences(
            db,
            session,
            actor_user_id=professor.id,
            actor_role="professor",
            change_source="smart-close-default",
            version=next_version,
            changed_at=now,
        )
    session.status = next_status
    session.closed_at = now
    session.expires_at = None
    session.latest_version = next_version
    db.commit()
    return {
        "session_id": session.id,
        "projection_key": session.projection_key,
        "projection_keys": projection_keys,
        "status": session.status,
        "version": session.latest_version,
        "occurred_at": _serialize_dt(session.closed_at),
        "course_code": course.course_code,
    }



def _enrolled_students_for_course(db: Session, course_id: int) -> list[tuple[int, str, str]]:
    return db.execute(
        select(User.id, User.student_id, User.name)
        .join(CourseEnrollment, CourseEnrollment.student_user_id == User.id)
        .where(CourseEnrollment.course_id == course_id, CourseEnrollment.status == "active", User.role == "student")
        .order_by(User.student_id.asc())
    ).all()



def _history_counts_for_course(db: Session, course_id: int) -> dict[int, int]:
    return {
        student_user_id: count
        for student_user_id, count in db.execute(
            select(AttendanceStatusAuditLog.student_user_id, func.count(AttendanceStatusAuditLog.id))
            .join(AttendanceSession, AttendanceSession.id == AttendanceStatusAuditLog.attendance_session_id)
            .where(AttendanceSession.course_id == course_id)
            .group_by(AttendanceStatusAuditLog.student_user_id)
        )
    }



def _records_for_session(db: Session, session_id: int) -> dict[tuple[int, str], AttendanceRecord]:
    records = db.scalars(select(AttendanceRecord).where(AttendanceRecord.attendance_session_id == session_id)).all()
    return {(record.student_user_id, record.projection_key): record for record in records}



def _student_slot_statuses(
    record_lookup: dict[tuple[int, str], AttendanceRecord],
    student_user_id: int,
    assignments: list[SessionSlotAssignment],
) -> dict[str, str | None]:
    return {
        assignment.projection_key: (
            record_lookup[(student_user_id, assignment.projection_key)].final_status
            if (student_user_id, assignment.projection_key) in record_lookup
            else None
        )
        for assignment in assignments
    }



def get_attendance_session_roster(db: Session, professor_id: str, session_id: int) -> dict[str, Any]:
    professor = get_professor_user(db, professor_id)
    session = db.scalar(select(AttendanceSession).where(AttendanceSession.id == session_id))
    if session is None:
        raise attendance_api_error(404, "ATTENDANCE_SESSION_NOT_FOUND", "attendance session not found", {"session_id": session_id})
    course = db.scalar(select(Course).where(Course.id == session.course_id))
    if course is None or course.professor_user_id != professor.id:
        raise attendance_api_error(403, "FORBIDDEN", "session does not belong to the authenticated professor")

    assignments = _session_assignments_for_one(db, session)
    slot_map = _projection_slot_lookup(db, course, professor)
    enrolled_rows = _enrolled_students_for_course(db, course.id)
    history_counts = _history_counts_for_course(db, course.id)
    record_lookup = _records_for_session(db, session.id)
    resolved_record_lookup = _records_for_sessions(db, [session.id])
    resolved_counts = _resolved_counts_for_session_slots(
        session,
        assignments,
        [student_user_id for student_user_id, _, _ in enrolled_rows],
        resolved_record_lookup,
    )
    students = []
    for student_user_id, student_login_id, student_name in enrolled_rows:
        anchor_record = record_lookup.get((student_user_id, session.projection_key))
        students.append(
            {
                "student_id": student_login_id,
                "student_name": student_name,
                "final_status": (
                    anchor_record.final_status
                    if anchor_record
                    else _resolved_slot_status(session, session.projection_key, student_user_id, resolved_record_lookup)
                ),
                "attendance_reason": anchor_record.attendance_reason if anchor_record else None,
                "history_count": history_counts.get(student_user_id, 0),
                "slot_statuses": _student_slot_statuses(record_lookup, student_user_id, assignments),
            }
        )
    aggregate = _aggregate_projection_counts(resolved_counts, assignments)
    return {
        "session": {
            "session_id": session.id,
            "projection_key": session.projection_key,
            "projection_keys": [assignment.projection_key for assignment in assignments],
            "included_slots": [_serialize_assignment(slot_map, assignment) for assignment in assignments],
            "mode": session.mode,
            "status": session.status,
            "expires_at": _serialize_dt(session.expires_at),
            "version": session.latest_version,
            "course_code": course.course_code,
            "bundle_slot_count": len(assignments),
        },
        "students": students,
        "aggregate": {
            "present": aggregate.get("present", 0),
            "late": aggregate.get("late", 0),
            "absent": aggregate.get("absent", 0),
            "official": aggregate.get("official", 0) + aggregate.get("sick", 0),
            "sick": aggregate.get("sick", 0),
        },
    }



def get_attendance_slot_roster_preview(db: Session, professor_id: str, course_code: str, projection_key: str) -> dict[str, Any]:
    professor, course = get_owned_course(db, professor_id, course_code)
    slot_map = _projection_slot_lookup(db, course, professor)
    slot = slot_map.get(projection_key)
    if slot is None:
        raise attendance_api_error(
            404,
            "SESSION_SLOT_INVALID",
            "projection key is not a valid slot for the course",
            {"course_code": course_code, "projection_key": projection_key},
        )

    latest_session = _latest_session_for_projection(db, projection_key)
    if latest_session is not None:
        assignments = _session_assignments_for_one(db, latest_session)
        record_lookup = _records_for_session(db, latest_session.id)
        resolved_record_lookup = _records_for_sessions(db, [latest_session.id])
        history_counts = _history_counts_for_course(db, course.id)
        enrolled_rows = _enrolled_students_for_course(db, course.id)
        resolved_counts = _resolved_counts_for_session_slots(
            latest_session,
            assignments,
            [student_user_id for student_user_id, _, _ in enrolled_rows],
            resolved_record_lookup,
        )
        students = []
        for student_user_id, student_login_id, student_name in enrolled_rows:
            record = record_lookup.get((student_user_id, projection_key))
            students.append(
                {
                    "student_id": student_login_id,
                    "student_name": student_name,
                    "final_status": (
                        record.final_status
                        if record
                        else _resolved_slot_status(latest_session, projection_key, student_user_id, resolved_record_lookup)
                    ),
                    "attendance_reason": record.attendance_reason if record else None,
                    "history_count": history_counts.get(student_user_id, 0),
                    "slot_statuses": _student_slot_statuses(record_lookup, student_user_id, assignments),
                }
            )
        aggregate = resolved_counts.get(projection_key, _counts_template())
        return {
            "session": {
                "session_id": latest_session.id,
                "projection_key": projection_key,
                "projection_keys": [assignment.projection_key for assignment in assignments],
                "included_slots": [_serialize_assignment(slot_map, assignment) for assignment in assignments],
                "mode": latest_session.mode,
                "status": latest_session.status,
                "expires_at": _serialize_dt(latest_session.expires_at),
                "version": latest_session.latest_version,
                "course_code": course.course_code,
                "bundle_session_id": latest_session.id,
                "bundle_anchor_projection_key": latest_session.projection_key,
            },
            "students": students,
            "aggregate": {
                "present": aggregate.get("present", 0),
                "late": aggregate.get("late", 0),
                "absent": aggregate.get("absent", 0),
                "official": aggregate.get("official", 0) + aggregate.get("sick", 0),
                "sick": aggregate.get("sick", 0),
            },
        }

    students = [
        {
            "student_id": student_id,
            "student_name": student_name,
            "final_status": None,
            "attendance_reason": None,
            "history_count": 0,
            "slot_statuses": {projection_key: None},
        }
        for _, student_id, student_name in _enrolled_students_for_course(db, course.id)
    ]
    return {
        "session": {
            "session_id": None,
            "projection_key": slot.projection_key,
            "projection_keys": [slot.projection_key],
            "included_slots": [
                {
                    "projection_key": slot.projection_key,
                    "classroom_code": slot.classroom_code,
                    "session_date": _serialize_date(slot.session_date),
                    "slot_start_at": _serialize_time(slot.slot_start_at),
                    "slot_end_at": _serialize_time(slot.slot_end_at),
                    "display_label": slot.display_label,
                    "period_label": slot.period_label,
                    "slot_order": 0,
                }
            ],
            "mode": None,
            "status": "unchecked",
            "expires_at": None,
            "version": 0,
            "course_code": course.course_code,
        },
        "students": students,
        "aggregate": {
            "present": 0,
            "late": 0,
            "absent": 0,
            "official": 0,
            "sick": 0,
        },
    }


def build_professor_student_attendance_stats(db: Session, professor_id: str, course_code: str) -> dict[str, Any]:
    professor, course = get_owned_course(db, professor_id, course_code)
    expire_stale_attendance_sessions(db, course_code)
    slots = _projection_slot_rows(db, course, professor)
    latest_sessions, assignments_by_session = _projection_lookup_by_session(db, course.id)
    _, record_lookup, enrolled_rows = _resolved_counts_by_session_for_course(
        db,
        course.id,
        latest_sessions,
        assignments_by_session,
    )

    rows = []
    for student_user_id, student_login_id, student_name in enrolled_rows:
        counts = _counts_template()
        for slot in slots:
            session = latest_sessions.get(slot.projection_key)
            if session is None:
                continue
            status = _resolved_slot_status(session, slot.projection_key, student_user_id, record_lookup)
            if status is None:
                continue
            counts[status] = counts.get(status, 0) + 1
        rows.append(
            {
                "student_id": student_login_id,
                "student_name": student_name,
                "present": counts["present"],
                "late": counts["late"],
                "absent": counts["absent"],
                "official": counts["official"] + counts["sick"],
                "sick": counts["sick"],
            }
        )

    return {
        "course_code": course.course_code,
        "course_title": course.title,
        "rows": rows,
    }


def build_student_attendance_semester_matrix(db: Session, student_id: str, course_code: str) -> dict[str, Any]:
    student = get_student_user(db, student_id)
    course = get_course_by_code(db, course_code)
    ensure_student_enrolled(db, student.id, course.id, student_id, course_code)
    professor = db.scalar(select(User).where(User.id == course.professor_user_id)) or User(name="담당 교수", role="professor", password="", professor_id="")
    expire_stale_attendance_sessions(db, course_code)
    slots = _projection_slot_rows(db, course, professor)
    latest_sessions, assignments_by_session = _projection_lookup_by_session(db, course.id)
    _, record_lookup, _ = _resolved_counts_by_session_for_course(
        db,
        course.id,
        latest_sessions,
        assignments_by_session,
    )

    weeks: dict[int, dict[str, Any]] = {}
    for slot in slots:
        session = latest_sessions.get(slot.projection_key)
        status = _resolved_slot_status(session, slot.projection_key, student.id, record_lookup) if session else None
        if session and (session.mode == "canceled" or session.status == "canceled"):
            cell_state = "canceled"
        elif session is None:
            cell_state = "upcoming"
        elif session.mode == "smart" and session.status == "active" and status is None:
            cell_state = "pending"
        elif status is None:
            cell_state = "upcoming"
        else:
            cell_state = status
        bucket = weeks.setdefault(
            slot.week_index,
            {
                "week_index": slot.week_index,
                "week_start": _serialize_date(slot.session_date - timedelta(days=slot.session_date.weekday())),
                "week_end": _serialize_date(slot.session_date - timedelta(days=slot.session_date.weekday()) + timedelta(days=6)),
                "slots": [],
            },
        )
        bucket["slots"].append(
            {
                "projection_key": slot.projection_key,
                "lesson_index_within_week": slot.lesson_index_within_week,
                "period_label": slot.period_label,
                "display_label": slot.display_label,
                "session_date": _serialize_date(slot.session_date),
                "status": cell_state,
            }
        )

    return {
        "course_code": course.course_code,
        "course_title": course.title,
        "student_id": student_id,
        "student_name": student.name,
        "weeks": [weeks[index] for index in sorted(weeks)],
    }



def _next_version(session: AttendanceSession) -> int:
    session.latest_version += 1
    return session.latest_version



def update_attendance_session_record(
    db: Session,
    professor_id: str,
    session_id: int,
    student_id: str,
    new_status: str,
    reason: str | None,
    projection_key: str | None = None,
) -> dict[str, Any]:
    if new_status not in FINAL_STATUSES:
        raise attendance_api_error(400, "INVALID_ATTENDANCE_STATUS", "invalid attendance status", {"status": new_status})
    normalized_reason = (reason or "").strip()

    professor = get_professor_user(db, professor_id)
    student = get_student_user(db, student_id)
    session = db.scalar(select(AttendanceSession).where(AttendanceSession.id == session_id))
    if session is None:
        raise attendance_api_error(404, "ATTENDANCE_SESSION_NOT_FOUND", "attendance session not found", {"session_id": session_id})
    course = db.scalar(select(Course).where(Course.id == session.course_id))
    if course is None or course.professor_user_id != professor.id:
        raise attendance_api_error(403, "FORBIDDEN", "session does not belong to the authenticated professor")
    if session.mode == "canceled" or session.status == "canceled":
        raise attendance_api_error(409, "SESSION_NOT_OPEN", "canceled sessions cannot accept roster mutations")
    ensure_student_enrolled(db, student.id, course.id, student_id, course.course_code)

    assignments = _session_assignments_for_one(db, session)
    if projection_key is not None:
        assignments = [assignment for assignment in assignments if assignment.projection_key == projection_key]
        if not assignments:
            raise attendance_api_error(
                404,
                "SESSION_SLOT_INVALID",
                "projection key is not part of the attendance session",
                {"session_id": session_id, "projection_key": projection_key},
            )

    audit_rows: list[AttendanceStatusAuditLog] = []
    changed_projection_keys: list[str] = []
    now = _utcnow()
    target_reason = normalized_reason or None
    for assignment in assignments:
        record = db.scalar(
            select(AttendanceRecord).where(
                AttendanceRecord.attendance_session_id == session.id,
                AttendanceRecord.projection_key == assignment.projection_key,
                AttendanceRecord.student_user_id == student.id,
            )
        )
        previous_status = record.final_status if record else None
        previous_reason = record.attendance_reason if record else None
        if previous_status == new_status and previous_reason == target_reason:
            continue
        if record is None:
            record = AttendanceRecord(
                attendance_session_id=session.id,
                projection_key=assignment.projection_key,
                student_user_id=student.id,
                final_status=new_status,
                attendance_reason=target_reason,
                finalized_by_user_id=professor.id,
                finalized_at=now,
            )
            db.add(record)
        else:
            record.final_status = new_status
            record.attendance_reason = target_reason
            record.finalized_by_user_id = professor.id
            record.finalized_at = now
        changed_projection_keys.append(assignment.projection_key)
        audit_rows.append(
            AttendanceStatusAuditLog(
                attendance_session_id=session.id,
                projection_key=assignment.projection_key,
                student_user_id=student.id,
                actor_user_id=professor.id,
                actor_role="professor",
                change_source="professor-slot-exception" if projection_key is not None else "professor-manual",
                previous_status=previous_status,
                new_status=new_status,
                reason=target_reason,
                changed_at=now,
                version=0,
            )
        )

    version = session.latest_version
    if audit_rows:
        version = _next_version(session)
        for row in audit_rows:
            row.version = version
            db.add(row)
        db.commit()

    return {
        "session_id": session.id,
        "projection_key": projection_key or session.projection_key,
        "projection_keys": changed_projection_keys or [assignment.projection_key for assignment in assignments],
        "student_id": student_id,
        "new_status": new_status,
        "reason": normalized_reason,
        "version": version,
        "course_code": course.course_code,
        "occurred_at": _serialize_dt(now),
        "changed": bool(audit_rows),
    }



def list_attendance_history(db: Session, professor_id: str, course_code: str, student_id: str) -> dict[str, Any]:
    _, course = get_owned_course(db, professor_id, course_code)
    student = get_student_user(db, student_id)
    ensure_student_enrolled(db, student.id, course.id, student_id, course_code)
    actor_rows = {
        user_id: {"name": name, "role": role, "login_id": student_id or professor_id or admin_id or ""}
        for user_id, name, role, student_id, professor_id, admin_id in db.execute(
            select(User.id, User.name, User.role, User.student_id, User.professor_id, User.admin_id)
        )
    }
    rows = db.execute(
        select(
            AttendanceStatusAuditLog.id,
            AttendanceStatusAuditLog.change_source,
            AttendanceStatusAuditLog.previous_status,
            AttendanceStatusAuditLog.new_status,
            AttendanceStatusAuditLog.reason,
            AttendanceStatusAuditLog.changed_at,
            AttendanceStatusAuditLog.version,
            AttendanceStatusAuditLog.actor_user_id,
            AttendanceStatusAuditLog.projection_key,
        )
        .join(AttendanceSession, AttendanceSession.id == AttendanceStatusAuditLog.attendance_session_id)
        .where(AttendanceSession.course_id == course.id, AttendanceStatusAuditLog.student_user_id == student.id)
        .order_by(AttendanceStatusAuditLog.changed_at.desc(), AttendanceStatusAuditLog.id.desc())
    )
    entries = []
    for audit_id, change_source, previous_status, new_status, reason, changed_at, version, actor_user_id, projection_key in rows:
        actor = actor_rows.get(actor_user_id, {"name": "알 수 없음", "role": "unknown", "login_id": ""})
        entries.append(
            {
                "audit_id": audit_id,
                "projection_key": projection_key,
                "change_source": change_source,
                "previous_status": previous_status,
                "new_status": new_status,
                "reason": reason,
                "changed_at": _serialize_dt(changed_at),
                "version": version,
                "actor_name": actor["name"],
                "actor_role": actor["role"],
                "actor_login_id": actor["login_id"],
            }
        )
    return {
        "student_id": student.student_id,
        "student_name": student.name,
        "course_code": course.course_code,
        "entries": entries,
    }



def _registered_devices_payload(db: Session, student: User) -> list[dict[str, str]]:
    devices = db.scalars(
        select(RegisteredDevice).where(RegisteredDevice.user_id == student.id, RegisteredDevice.status == "active")
    ).all()
    return [{"mac": device.mac_address, "label": device.label} for device in devices]



def _presence_eligibility_for_assignment(
    db: Session,
    presence_client: PresenceClient,
    student: User,
    course: Course,
    assignment: SessionSlotAssignment,
    registered_devices: list[dict[str, str]],
) -> dict[str, Any]:
    classroom = db.scalar(select(Classroom).where(Classroom.id == assignment.classroom_id))
    if not registered_devices:
        return {
            "eligible": False,
            "reason_code": "DEVICE_NOT_REGISTERED",
            "matched_device_mac": None,
            "observed_at": None,
            "snapshot_age_seconds": None,
            "evidence": {},
        }
    payload = presence_client.check_eligibility(
        student_id=student.student_id or "",
        course_id=course.course_code,
        classroom_id=classroom.classroom_code if classroom else "",
        purpose="attendance",
        classroom_networks=[
            {
                "apId": network.ap_id,
                "ssid": network.ssid,
                "signalThresholdDbm": network.signal_threshold_dbm,
            }
            for network in db.scalars(select(ClassroomNetwork).where(ClassroomNetwork.classroom_id == assignment.classroom_id))
        ],
        registered_devices=registered_devices,
    )
    return {
        "eligible": bool(payload.get("eligible")),
        "reason_code": payload.get("reasonCode", "UNKNOWN"),
        "matched_device_mac": payload.get("matchedDeviceMac"),
        "observed_at": payload.get("observedAt"),
        "snapshot_age_seconds": payload.get("snapshotAgeSeconds"),
        "evidence": payload.get("evidence", {}),
    }



def list_student_active_attendance_sessions(
    db: Session,
    presence_client: PresenceClient,
    student_id: str,
    course_code: str,
) -> dict[str, Any]:
    student = get_student_user(db, student_id)
    course = get_course_by_code(db, course_code)
    ensure_student_enrolled(db, student.id, course.id, student_id, course_code)
    expire_stale_attendance_sessions(db, course_code)
    professor = db.scalar(select(User).where(User.id == course.professor_user_id)) or User(name="담당 교수", role="professor", password="", professor_id="")
    slot_map = _projection_slot_lookup(db, course, professor)
    sessions = db.scalars(
        select(AttendanceSession)
        .where(
            AttendanceSession.course_id == course.id,
            AttendanceSession.status == "active",
            AttendanceSession.mode == "smart",
        )
        .order_by(AttendanceSession.session_date.asc(), AttendanceSession.slot_start_at.asc(), AttendanceSession.id.asc())
    ).all()
    assignments_by_session = _session_slot_assignments(db, sessions)
    registered_devices = _registered_devices_payload(db, student)
    serialized_sessions = []
    for session in sessions:
        assignments = assignments_by_session.get(session.id, [_fallback_session_assignment(session)])
        eligibilities = [
            {
                "projection_key": assignment.projection_key,
                "eligibility": _presence_eligibility_for_assignment(
                    db,
                    presence_client,
                    student,
                    course,
                    assignment,
                    registered_devices,
                ),
            }
            for assignment in assignments
        ]
        changed_or_present = [item for item in eligibilities if item["eligibility"]["eligible"]]
        anchor_slot = slot_map.get(session.projection_key)
        serialized_sessions.append(
            {
                "session_id": session.id,
                "projection_key": session.projection_key,
                "projection_keys": [assignment.projection_key for assignment in assignments],
                "included_slots": [_serialize_assignment(slot_map, assignment) for assignment in assignments],
                "display_label": anchor_slot.display_label if anchor_slot else session.projection_key,
                "session_date": _serialize_date(session.session_date),
                "slot_start_at": _serialize_time(session.slot_start_at),
                "slot_end_at": _serialize_time(session.slot_end_at),
                "expires_at": _serialize_dt(session.expires_at),
                "can_check_in": bool(changed_or_present),
                "eligibility": {
                    "eligible_slot_count": len(changed_or_present),
                    "rejected_slot_count": len(eligibilities) - len(changed_or_present),
                    "per_slot": eligibilities,
                },
                "version": session.latest_version,
            }
        )
    return {
        "course_code": course.course_code,
        "student_id": student_id,
        "sessions": serialized_sessions,
    }



def student_attendance_check_in(db: Session, presence_client: PresenceClient, student_id: str, session_id: int) -> dict[str, Any]:
    student = get_student_user(db, student_id)
    session = db.scalar(select(AttendanceSession).where(AttendanceSession.id == session_id))
    if session is None:
        raise attendance_api_error(404, "ATTENDANCE_SESSION_NOT_FOUND", "attendance session not found", {"session_id": session_id})
    course = db.scalar(select(Course).where(Course.id == session.course_id))
    if course is None:
        raise attendance_api_error(404, "COURSE_NOT_FOUND", "course not found")
    ensure_student_enrolled(db, student.id, course.id, student_id, course.course_code)
    expire_stale_attendance_sessions(db, course.course_code)
    db.refresh(session)
    if session.mode != "smart" or session.status != "active":
        raise attendance_api_error(409, "SESSION_NOT_OPEN", "smart attendance session is not open", {"session_id": session_id})

    assignments = _session_assignments_for_one(db, session)
    registered_devices = _registered_devices_payload(db, student)
    now = _utcnow()
    pending_audits: list[AttendanceStatusAuditLog] = []
    changed_projection_keys: list[str] = []
    already_present_count = 0
    rejected_count = 0
    per_slot_results: list[dict[str, Any]] = []

    for assignment in assignments:
        eligibility = _presence_eligibility_for_assignment(
            db,
            presence_client,
            student,
            course,
            assignment,
            registered_devices,
        )
        if not eligibility["eligible"]:
            rejected_count += 1
            per_slot_results.append(
                {
                    "projection_key": assignment.projection_key,
                    "result": "rejected",
                    "reason_code": eligibility["reason_code"],
                    "eligibility": eligibility,
                }
            )
            continue

        record = db.scalar(
            select(AttendanceRecord).where(
                AttendanceRecord.attendance_session_id == session.id,
                AttendanceRecord.projection_key == assignment.projection_key,
                AttendanceRecord.student_user_id == student.id,
            )
        )
        if record is not None and record.final_status == "present":
            already_present_count += 1
            per_slot_results.append(
                {
                    "projection_key": assignment.projection_key,
                    "result": "already-present",
                    "reason_code": "OK",
                    "eligibility": eligibility,
                }
            )
            continue

        previous_status = record.final_status if record else None
        if record is None:
            record = AttendanceRecord(
                attendance_session_id=session.id,
                projection_key=assignment.projection_key,
                student_user_id=student.id,
                final_status="present",
                attendance_reason="학생 self check-in",
                finalized_by_user_id=student.id,
                finalized_at=now,
            )
            db.add(record)
        else:
            record.final_status = "present"
            record.attendance_reason = record.attendance_reason or "학생 self check-in"
            record.finalized_by_user_id = student.id
            record.finalized_at = now
        changed_projection_keys.append(assignment.projection_key)
        pending_audits.append(
            AttendanceStatusAuditLog(
                attendance_session_id=session.id,
                projection_key=assignment.projection_key,
                student_user_id=student.id,
                actor_user_id=student.id,
                actor_role="student",
                change_source="self-checkin",
                previous_status=previous_status,
                new_status="present",
                reason="학생 self check-in",
                changed_at=now,
                version=0,
            )
        )
        per_slot_results.append(
            {
                "projection_key": assignment.projection_key,
                "result": "checked-in",
                "reason_code": "OK",
                "eligibility": eligibility,
            }
        )

    version = session.latest_version
    if pending_audits:
        version = _next_version(session)
        for audit in pending_audits:
            audit.version = version
            db.add(audit)
        db.commit()

    changed_count = len(changed_projection_keys)
    return {
        "code": "ATTENDANCE_CHECK_IN_OK",
        "session_id": session.id,
        "projection_key": session.projection_key,
        "projection_keys": [assignment.projection_key for assignment in assignments],
        "changed_projection_keys": changed_projection_keys,
        "student_id": student_id,
        "status": "present" if changed_count or already_present_count else "rejected",
        "version": version,
        "occurred_at": _serialize_dt(now),
        "course_code": course.course_code,
        "idempotent": changed_count == 0 and already_present_count > 0 and rejected_count == 0,
        "changed_count": changed_count,
        "already_present_count": already_present_count,
        "rejected_count": rejected_count,
        "results": per_slot_results,
    }


def attendance_event_payload(
    *,
    event_type: str,
    course_code: str,
    projection_keys: list[str] | None = None,
    session_ids: list[int] | None = None,
    version: int | None = None,
    changed_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "event_type": event_type,
        "course_code": course_code,
        "projection_keys": projection_keys or [],
        "session_ids": session_ids or [],
        "version": version,
        "occurred_at": _serialize_dt(_utcnow()),
        "changed_payload": changed_payload or {},
    }
