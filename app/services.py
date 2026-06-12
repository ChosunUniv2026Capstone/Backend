import csv
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import datetime as datetime_module
from io import BytesIO, StringIO
import hashlib
import math
import re
import secrets
from uuid import uuid4

from fastapi import HTTPException, UploadFile
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.orm import Session

from app.attendance import build_professor_attendance_csv_table
from app.config import get_settings
from app.models import (
    AccessPoint,
    AccessPointInterface,
    Classroom,
    ClassroomNetwork,
    Course,
    CourseEnrollment,
    CourseSchedule,
    Exam,
    ExamQuestion,
    ExamQuestionAttachment,
    ExamQuestionOption,
    ExamSubmission,
    ExamSubmissionAnswer,
    LearningItem,
    LearningItemAttachment,
    Notice,
    NoticeAttachment,
    PresenceEligibilityLog,
    RegisteredDevice,
    ReportExport,
    User,
)
from app.presence_client import (
    PresenceClient,
    is_presence_dependency_unavailable,
    presence_dependency_unavailable_result,
)
from app.schemas import DeviceCreate
from app.storage import get_storage_backend, get_storage_backend_for_metadata, spool_limited_upload

MAC_PATTERN = re.compile(r"^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$")
MAX_DEVICES_PER_STUDENT = 5
FILENAME_SANITIZE_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")
settings = get_settings()


def _utcnow_service() -> datetime:
    return datetime.now(UTC)


def hash_ap_token(token: str) -> str:
    return hashlib.sha256(f"{settings.ap_token_hash_secret}:{token}".encode("utf-8")).hexdigest()


def _access_point_or_404(db: Session, collector_ap_id: str) -> AccessPoint:
    access_point = db.scalar(select(AccessPoint).where(AccessPoint.collector_ap_id == collector_ap_id))
    if access_point is None:
        raise HTTPException(status_code=404, detail={"code": "ACCESS_POINT_NOT_FOUND", "message": "access point not found", "details": {"collector_ap_id": collector_ap_id}})
    return access_point


def list_access_points(db: Session) -> list[dict]:
    rows = db.execute(
        select(AccessPoint, AccessPointInterface, ClassroomNetwork, Classroom)
        .outerjoin(AccessPointInterface, AccessPointInterface.access_point_id == AccessPoint.id)
        .outerjoin(ClassroomNetwork, ClassroomNetwork.id == AccessPointInterface.classroom_network_id)
        .outerjoin(Classroom, Classroom.id == ClassroomNetwork.classroom_id)
        .order_by(AccessPoint.collector_ap_id.asc(), AccessPointInterface.interface_id.asc())
    ).all()
    by_id: dict[int, dict] = {}
    for access_point, interface, network, classroom in rows:
        entry = by_id.setdefault(
            access_point.id,
            {
                "id": access_point.id,
                "collector_ap_id": access_point.collector_ap_id,
                "label": access_point.label,
                "management_ip": access_point.management_ip,
                "tailnet_ip": access_point.tailnet_ip,
                "status": access_point.status,
                "token_configured": bool(access_point.token_hash) and access_point.token_revoked_at is None,
                "token_version": access_point.token_version,
                "token_revoked_at": access_point.token_revoked_at,
                "last_rotated_at": access_point.last_rotated_at,
                "interfaces": [],
            },
        )
        if interface is not None and network is not None and classroom is not None:
            entry["interfaces"].append(
                {
                    "interface_id": interface.interface_id,
                    "bssid": interface.bssid,
                    "ssid": interface.ssid or network.ssid,
                    "classroom_code": classroom.classroom_code,
                    "classroom_network_id": network.id,
                    "classroom_network_ap_id": network.ap_id,
                    "signal_threshold_dbm": network.signal_threshold_dbm,
                    "collection_mode": network.collection_mode,
                }
            )
    return list(by_id.values())


def issue_access_point_token(db: Session, collector_ap_id: str) -> dict:
    access_point = _access_point_or_404(db, collector_ap_id)
    token = secrets.token_urlsafe(32)
    now = _utcnow_service()
    access_point.token_hash = hash_ap_token(token)
    access_point.token_version = (access_point.token_version or 0) + 1
    access_point.token_revoked_at = None
    access_point.last_rotated_at = now
    access_point.updated_at = now
    db.add(access_point)
    db.commit()
    db.refresh(access_point)
    return {
        "collector_ap_id": access_point.collector_ap_id,
        "token": token,
        "token_version": access_point.token_version,
        "last_rotated_at": access_point.last_rotated_at,
    }


def revoke_access_point_token(db: Session, collector_ap_id: str) -> dict:
    access_point = _access_point_or_404(db, collector_ap_id)
    now = _utcnow_service()
    access_point.token_revoked_at = now
    access_point.updated_at = now
    db.add(access_point)
    db.commit()
    return {"collector_ap_id": access_point.collector_ap_id, "token_revoked_at": access_point.token_revoked_at}


def build_presence_registry(db: Session) -> dict:
    access_points = []
    for entry in list_access_points(db):
        ap = _access_point_or_404(db, entry["collector_ap_id"])
        access_points.append(
            {
                "collectorApId": ap.collector_ap_id,
                "label": ap.label,
                "managementIp": ap.management_ip,
                "tailnetIp": ap.tailnet_ip,
                "status": ap.status,
                "tokenHash": ap.token_hash,
                "tokenVersion": ap.token_version,
                "tokenRevokedAt": ap.token_revoked_at.isoformat() if ap.token_revoked_at else None,
                "lastRotatedAt": ap.last_rotated_at.isoformat() if ap.last_rotated_at else None,
                "interfaces": [
                    {
                        "interfaceId": iface["interface_id"],
                        "bssid": iface["bssid"],
                        "ssid": iface["ssid"],
                        "classroomId": iface["classroom_code"],
                        "classroomNetworkId": iface["classroom_network_id"],
                        "classroomNetworkApId": iface["classroom_network_ap_id"],
                        "signalThresholdDbm": iface["signal_threshold_dbm"],
                        "collectionMode": iface["collection_mode"],
                    }
                    for iface in entry["interfaces"]
                ],
            }
        )
    return {"accessPoints": access_points}


@dataclass(frozen=True)
class ObjectDownload:
    storage_key: str
    filename: str
    media_type: str | None
    file_size_bytes: int
    storage_provider: str | None = None
    bucket_name: str | None = None


def normalize_mac(mac_address: str) -> str:
    normalized = mac_address.strip().lower()
    if not MAC_PATTERN.match(normalized):
        raise HTTPException(status_code=400, detail="invalid MAC address format")
    return normalized


def _normalize_original_filename(filename: str | None) -> str:
    candidate = (filename or "attachment").replace("\x00", "").strip()
    if not candidate:
        candidate = "attachment"
    return candidate.replace("/", "_").replace("\\", "_")[:255] or "attachment"


def _sanitize_filename(filename: str | None) -> str:
    sanitized = FILENAME_SANITIZE_PATTERN.sub("_", _normalize_original_filename(filename))
    return sanitized[:180] or "attachment"


def _current_storage_metadata() -> tuple[str, str]:
    backend = get_storage_backend()
    provider = backend.provider if backend.provider in {"local", "s3"} else "local"
    bucket_name = backend.bucket_name or ("local" if provider == "local" else settings.object_storage_bucket)
    return provider, bucket_name


def _store_domain_upload(upload: UploadFile, *, prefix: str) -> dict:
    original_filename = _normalize_original_filename(upload.filename)
    stored_filename = f"{uuid4().hex}_{_sanitize_filename(original_filename)}"
    storage_key = f"{prefix.rstrip('/')}/{stored_filename}"
    mime_type = upload.content_type or "application/octet-stream"
    try:
        spooled_file, file_size = spool_limited_upload(
            upload,
            max_bytes=settings.assignment_upload_max_file_size_bytes,
            chunk_size=settings.object_storage_proxy_chunk_size_bytes,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "OBJECT_UPLOAD_TOO_LARGE",
                "message": "uploaded file is too large",
                "details": {"filename": original_filename, "max_file_size_bytes": settings.assignment_upload_max_file_size_bytes},
            },
        ) from exc
    with spooled_file:
        get_storage_backend().put_object(storage_key, spooled_file, content_type=mime_type)
    storage_provider, bucket_name = _current_storage_metadata()
    return {
        "original_filename": original_filename,
        "stored_filename": stored_filename,
        "mime_type": mime_type,
        "file_size_bytes": file_size,
        "storage_provider": storage_provider,
        "bucket_name": bucket_name,
        "storage_key": storage_key,
    }


def _store_bytes_object(*, content: bytes, filename: str, mime_type: str, prefix: str) -> dict:
    original_filename = _normalize_original_filename(filename)
    stored_filename = f"{uuid4().hex}_{_sanitize_filename(original_filename)}"
    storage_key = f"{prefix.rstrip('/')}/{stored_filename}"
    get_storage_backend().put_object(storage_key, BytesIO(content), content_type=mime_type)
    storage_provider, bucket_name = _current_storage_metadata()
    return {
        "original_filename": original_filename,
        "stored_filename": stored_filename,
        "mime_type": mime_type,
        "file_size_bytes": len(content),
        "storage_provider": storage_provider,
        "bucket_name": bucket_name,
        "storage_key": storage_key,
    }


def _attachment_payload(attachment, *, purpose: str | None = None, question_id: int | None = None) -> dict:
    payload = {
        "id": attachment.id,
        "original_filename": attachment.original_filename,
        "mime_type": attachment.mime_type,
        "file_size_bytes": attachment.file_size_bytes,
        "uploaded_at": attachment.created_at,
        "storage_provider": getattr(attachment, "storage_provider", None),
        "bucket_name": getattr(attachment, "bucket_name", None),
    }
    if purpose is not None:
        payload["purpose"] = purpose
    if question_id is not None:
        payload["question_id"] = question_id
    return payload


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
        if existing.user_id == user.id and existing.status == "deleted":
            existing.label = payload.label.strip()
            existing.status = "active"
            db.commit()
            db.refresh(existing)
            return existing
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
            func.min(Classroom.classroom_code),
        )
        .join(CourseEnrollment, CourseEnrollment.course_id == Course.id)
        .join(User, User.id == Course.professor_user_id, isouter=True)
        .join(CourseSchedule, CourseSchedule.course_id == Course.id, isouter=True)
        .join(Classroom, Classroom.id == CourseSchedule.classroom_id, isouter=True)
        .where(
            CourseEnrollment.student_user_id == user.id,
            CourseEnrollment.status == "active",
        )
        .group_by(Course.id, Course.course_code, Course.title, User.name)
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
        select(Course.id, Course.course_code, Course.title, func.min(Classroom.classroom_code))
        .join(CourseSchedule, CourseSchedule.course_id == Course.id, isouter=True)
        .join(Classroom, Classroom.id == CourseSchedule.classroom_id, isouter=True)
        .where(Course.professor_user_id == professor.id)
        .group_by(Course.id, Course.course_code, Course.title)
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


def _serialize_exam_summary(exam: Exam, *, attempts_used: int | None = None) -> dict:
    payload = {
        "id": exam.id,
        "title": exam.title,
        "exam_type": exam.exam_type,
        "status": exam.status,
        "starts_at": exam.starts_at,
        "ends_at": exam.ends_at,
        "duration_minutes": exam.duration_minutes,
        "requires_presence": exam.requires_presence,
        "max_attempts": exam.max_attempts,
    }
    if attempts_used is not None:
        payload["attempts_used"] = attempts_used
    return payload


def _load_attempt_count_index(db: Session, student_user_id: int, exam_ids: list[int]) -> dict[int, int]:
    if not exam_ids:
        return {}
    rows = db.execute(
        select(ExamSubmission.exam_id, func.count(ExamSubmission.id))
        .where(
            ExamSubmission.student_user_id == student_user_id,
            ExamSubmission.exam_id.in_(exam_ids),
        )
        .group_by(ExamSubmission.exam_id)
    )
    return {int(exam_id): int(count) for exam_id, count in rows}


def list_student_exams(db: Session, student_user_id: int, course_id: int) -> list[dict]:
    student_visible_statuses = ("published", "open", "closed")

    def count_questions(exam_id: int) -> int:
        return int(db.scalar(select(func.count(ExamQuestion.id)).where(ExamQuestion.exam_id == exam_id)) or 0)

    def count_attempts(exam_id: int) -> int:
        return int(db.scalar(select(func.count(ExamSubmission.id)).where(ExamSubmission.exam_id == exam_id)) or 0)

    def get_latest_submission(exam_id: int) -> ExamSubmission | None:
        return db.scalar(
            select(ExamSubmission)
            .where(
                ExamSubmission.exam_id == exam_id,
                ExamSubmission.student_user_id == student_user_id,
            )
            .order_by(ExamSubmission.attempt_no.desc(), ExamSubmission.id.desc())
        )

    def build_attempt_summary(submission: ExamSubmission | None) -> dict | None:
        if submission is None:
            return None
        answered_count = int(
            db.scalar(
                select(func.count(ExamSubmissionAnswer.id)).where(
                    ExamSubmissionAnswer.submission_id == submission.id,
                    or_(
                        ExamSubmissionAnswer.selected_option_id.is_not(None),
                        ExamSubmissionAnswer.answer_text.is_not(None),
                    ),
                )
            ) or 0
        )
        return {
            "id": submission.id,
            "attempt_no": submission.attempt_no,
            "status": submission.status,
            "started_at": submission.started_at,
            "submitted_at": submission.submitted_at,
            "expires_at": submission.expires_at,
            "score": float(submission.score) if submission.score is not None else None,
            "total_count": count_questions(submission.exam_id),
            "answered_count": answered_count,
        }

    def build_availability(exam: Exam, submission: ExamSubmission | None) -> dict:
        now = datetime.now(UTC)
        if exam.status == "draft":
            return {"code": "draft", "label": "draft", "can_start": False, "can_submit": False}
        if exam.status == "closed" or now >= exam.ends_at:
            return {"code": "closed", "label": "closed", "can_start": False, "can_submit": False}
        if now < exam.starts_at:
            return {"code": "upcoming", "label": "upcoming", "can_start": False, "can_submit": False}
        if submission and submission.status == "in_progress":
            return {"code": "in_progress", "label": "in_progress", "can_start": False, "can_submit": True}
        if submission and submission.status in {"submitted", "auto_submitted", "graded"}:
            return {"code": "submitted", "label": "submitted", "can_start": False, "can_submit": False}
        if not exam.late_entry_allowed and now > exam.starts_at:
            return {"code": "late_entry_blocked", "label": "late_entry_blocked", "can_start": False, "can_submit": False}
        return {"code": "available", "label": "available", "can_start": True, "can_submit": False}

    def serialize_summary(exam: Exam, *, attempts_used: int = 0, submission: ExamSubmission | None = None) -> dict:
        return {
            "id": exam.id,
            "title": exam.title,
            "description": exam.description,
            "exam_type": exam.exam_type,
            "status": exam.status,
            "starts_at": exam.starts_at,
            "ends_at": exam.ends_at,
            "duration_minutes": exam.duration_minutes,
            "requires_presence": exam.requires_presence,
            "late_entry_allowed": exam.late_entry_allowed,
            "auto_submit_enabled": exam.auto_submit_enabled,
            "shuffle_questions": exam.shuffle_questions,
            "shuffle_options": exam.shuffle_options,
            "max_attempts": exam.max_attempts,
            "question_count": count_questions(exam.id),
            "attempt_count": count_attempts(exam.id),
            "attempts_used": attempts_used,
            "availability": build_availability(exam, submission),
            "attempt": build_attempt_summary(submission),
        }

    exams = list(
        db.scalars(
            select(Exam)
            .where(
                Exam.course_id == course_id,
                Exam.status.in_(student_visible_statuses),
            )
            .order_by(Exam.starts_at.asc(), Exam.id.asc())
        )
    )
    attempt_counts = _load_attempt_count_index(db, student_user_id, [exam.id for exam in exams])
    return [serialize_summary(exam, attempts_used=attempt_counts.get(exam.id, 0), submission=get_latest_submission(exam.id)) for exam in exams]


def _exam_question_attachment_index(db: Session, question_ids: list[int]) -> dict[int, list[dict]]:
    if not question_ids:
        return {}
    attachments = list(
        db.scalars(
            select(ExamQuestionAttachment)
            .where(ExamQuestionAttachment.question_id.in_(question_ids))
            .order_by(ExamQuestionAttachment.question_id.asc(), ExamQuestionAttachment.created_at.asc(), ExamQuestionAttachment.id.asc())
        )
    )
    index: dict[int, list[dict]] = {}
    for attachment in attachments:
        index.setdefault(attachment.question_id, []).append(
            _attachment_payload(attachment, purpose=attachment.attachment_role, question_id=attachment.question_id)
        )
    return index


def get_student_exam_detail(db: Session, student_user_id: int, course_id: int, exam_id: int) -> dict:
    student_visible_statuses = ("published", "open", "closed")

    exam = db.scalar(
        select(Exam).where(
            Exam.id == exam_id,
            Exam.course_id == course_id,
            Exam.status.in_(student_visible_statuses),
        )
    )
    if not exam:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "EXAM_NOT_FOUND",
                "message": "exam not found",
                "details": {"exam_id": exam_id},
            },
        )

    attempts_used = int(
        db.scalar(
            select(func.count(ExamSubmission.id)).where(
                ExamSubmission.exam_id == exam.id,
                ExamSubmission.student_user_id == student_user_id,
            )
        ) or 0
    )
    latest_submission = db.scalar(
        select(ExamSubmission)
        .where(
            ExamSubmission.exam_id == exam.id,
            ExamSubmission.student_user_id == student_user_id,
        )
        .order_by(ExamSubmission.attempt_no.desc(), ExamSubmission.id.desc())
    )

    summary = next(
        exam_summary
        for exam_summary in list_student_exams(db, student_user_id, course_id)
        if exam_summary["id"] == exam.id
    )

    questions: list[dict] = []
    if latest_submission is not None:
        loaded_questions = list(
            db.scalars(
                select(ExamQuestion)
                .where(ExamQuestion.exam_id == exam.id)
                .order_by(ExamQuestion.question_order.asc(), ExamQuestion.id.asc())
            )
        )
        if exam.shuffle_questions:
            loaded_questions = sorted(
                loaded_questions,
                key=lambda question: (question.id * 37 + latest_submission.id * 13) % 997,
            )
        question_ids = [question.id for question in loaded_questions]
        options = list(
            db.scalars(
                select(ExamQuestionOption)
                .where(ExamQuestionOption.question_id.in_(question_ids))
                .order_by(ExamQuestionOption.question_id.asc(), ExamQuestionOption.option_order.asc(), ExamQuestionOption.id.asc())
            )
        ) if question_ids else []
        answers = list(
            db.scalars(
                select(ExamSubmissionAnswer).where(ExamSubmissionAnswer.submission_id == latest_submission.id)
            )
        )
        attachment_index = _exam_question_attachment_index(db, question_ids)
        option_index: dict[int, list[dict]] = {}
        answer_index = {answer.question_id: answer for answer in answers}
        for option in options:
            option_index.setdefault(option.question_id, []).append(
                {
                    "id": option.id,
                    "option_order": option.option_order,
                    "option_text": option.option_text,
                }
            )
        questions = [
            {
                "id": question.id,
                "question_order": index,
                "question_type": question.question_type,
                "prompt": question.prompt,
                "points": float(question.points),
                "explanation": question.explanation,
                "is_required": question.is_required,
                "selected_option_id": answer_index.get(question.id).selected_option_id if question.id in answer_index else None,
                "options": option_index.get(question.id, []),
                "attachments": attachment_index.get(question.id, []),
            }
            for index, question in enumerate(loaded_questions, start=1)
        ]

    return {
        **summary,
        "attempts_used": attempts_used,
        "questions": questions,
    }


def _validate_professor_exam_payload(payload: dict) -> None:
    if payload["ends_at"] <= payload["starts_at"]:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "EXAM_INVALID_WINDOW",
                "message": "exam end time must be later than start time",
                "details": {
                    "starts_at": payload["starts_at"].isoformat(),
                    "ends_at": payload["ends_at"].isoformat(),
                },
            },
        )

    questions = payload.get("questions") or []
    if not questions:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "EXAM_INVALID_PAYLOAD",
                "message": "at least one question is required",
                "details": {},
            },
        )

    for index, question in enumerate(questions, start=1):
        if question["question_type"] not in {"multiple_choice", "true_false"}:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "EXAM_INVALID_PAYLOAD",
                    "message": "unsupported question type",
                    "details": {"question_order": index},
                },
            )
        options = question.get("options") or []
        if len(options) < 2 or sum(1 for option in options if option.get("is_correct")) != 1:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "EXAM_INVALID_PAYLOAD",
                    "message": "objective questions require at least two options and exactly one correct option",
                    "details": {"question_order": index},
                },
            )


def _replace_professor_exam_questions(*, db: Session, exam_id: int, questions: list[dict]) -> None:
    existing_question_ids = list(
        db.scalars(select(ExamQuestion.id).where(ExamQuestion.exam_id == exam_id))
    )
    if existing_question_ids:
        db.execute(delete(ExamQuestionOption).where(ExamQuestionOption.question_id.in_(existing_question_ids)))
        db.execute(delete(ExamQuestion).where(ExamQuestion.id.in_(existing_question_ids)))
        db.flush()

    for question_order, question in enumerate(questions, start=1):
        exam_question = ExamQuestion(
            exam_id=exam_id,
            question_order=question_order,
            question_type=question["question_type"],
            prompt=question["prompt"].strip(),
            points=question["points"],
            explanation=(question.get("explanation") or "").strip() or None,
            is_required=question.get("is_required", True),
        )
        db.add(exam_question)
        db.flush()
        for option_order, option in enumerate(question.get("options") or [], start=1):
            db.add(
                ExamQuestionOption(
                    question_id=exam_question.id,
                    option_order=option_order,
                    option_text=option["option_text"].strip(),
                    is_correct=bool(option.get("is_correct")),
                )
            )


def _build_professor_submission_snapshot(*, db: Session, exam: Exam) -> tuple[dict, list[dict]]:
    max_score = float(
        db.scalar(select(func.coalesce(func.sum(ExamQuestion.points), 0)).where(ExamQuestion.exam_id == exam.id)) or 0
    )
    total_count = int(db.scalar(select(func.count(ExamQuestion.id)).where(ExamQuestion.exam_id == exam.id)) or 0)

    enrolled_students = list(
        db.execute(
            select(User.id, User.student_id, User.name)
            .join(CourseEnrollment, CourseEnrollment.student_user_id == User.id)
            .where(
                CourseEnrollment.course_id == exam.course_id,
                User.role == "student",
            )
            .order_by(User.student_id.asc(), User.id.asc())
        )
    )

    latest_attempt_rows = list(
        db.scalars(
            select(ExamSubmission)
            .where(ExamSubmission.exam_id == exam.id)
            .order_by(ExamSubmission.student_user_id.asc(), ExamSubmission.attempt_no.desc(), ExamSubmission.id.desc())
        )
    )
    latest_attempt_by_student: dict[int, ExamSubmission] = {}
    for submission in latest_attempt_rows:
        latest_attempt_by_student.setdefault(submission.student_user_id, submission)

    submission_ids = [submission.id for submission in latest_attempt_by_student.values()]
    answered_count_index: dict[int, int] = {}
    if submission_ids:
        answered_rows = db.execute(
            select(
                ExamSubmissionAnswer.submission_id,
                func.count(ExamSubmissionAnswer.id),
            )
            .where(
                ExamSubmissionAnswer.submission_id.in_(submission_ids),
                or_(
                    ExamSubmissionAnswer.selected_option_id.is_not(None),
                    ExamSubmissionAnswer.answer_text.is_not(None),
                ),
            )
            .group_by(ExamSubmissionAnswer.submission_id)
        )
        answered_count_index = {int(submission_id): int(count) for submission_id, count in answered_rows}

    submissions: list[dict] = []
    started_students = 0
    submitted_students = 0
    scored_values: list[float] = []
    for student_user_id, student_login_id, student_name in enrolled_students:
        latest_attempt = latest_attempt_by_student.get(student_user_id)
        if latest_attempt is None:
            submissions.append(
                {
                    "student_id": student_login_id,
                    "student_name": student_name,
                    "status": "not_started",
                    "attempt_no": None,
                    "answered_count": 0,
                    "started_at": None,
                    "submitted_at": None,
                    "score": None,
                    "max_score": max_score,
                    "total_count": total_count,
                }
            )
            continue

        started_students += 1
        if latest_attempt.status in {"submitted", "auto_submitted", "graded"}:
            submitted_students += 1
        if latest_attempt.score is not None:
            scored_values.append(float(latest_attempt.score))
        submissions.append(
            {
                "student_id": student_login_id,
                "student_name": student_name,
                "status": latest_attempt.status,
                "attempt_no": latest_attempt.attempt_no,
                "answered_count": answered_count_index.get(latest_attempt.id, 0),
                "started_at": latest_attempt.started_at,
                "submitted_at": latest_attempt.submitted_at,
                "score": float(latest_attempt.score) if latest_attempt.score is not None else None,
                "max_score": max_score,
                "total_count": total_count,
            }
        )

    overview = {
        "total_students": len(enrolled_students),
        "started_students": started_students,
        "submitted_students": submitted_students,
        "not_started_students": len(enrolled_students) - started_students,
        "average_score": round(sum(scored_values) / len(scored_values), 2) if scored_values else None,
        "max_score": max_score,
    }
    return overview, submissions


def _delete_professor_exam_graph(*, db: Session, exam_id: int) -> None:
    question_ids = list(db.scalars(select(ExamQuestion.id).where(ExamQuestion.exam_id == exam_id)))
    submission_ids = list(db.scalars(select(ExamSubmission.id).where(ExamSubmission.exam_id == exam_id)))
    if submission_ids:
        db.execute(delete(ExamSubmissionAnswer).where(ExamSubmissionAnswer.submission_id.in_(submission_ids)))
    if question_ids:
        db.execute(delete(ExamQuestionOption).where(ExamQuestionOption.question_id.in_(question_ids)))
        db.execute(delete(ExamQuestion).where(ExamQuestion.id.in_(question_ids)))
    if submission_ids:
        db.execute(delete(ExamSubmission).where(ExamSubmission.id.in_(submission_ids)))
    db.execute(delete(ExamSubmissionAnswer).where(ExamSubmissionAnswer.exam_id == exam_id))
    db.flush()


def list_professor_exams(db: Session, course_id: int) -> list[dict]:
    exams = list(
        db.scalars(
            select(Exam)
            .where(Exam.course_id == course_id)
            .order_by(Exam.starts_at.asc(), Exam.id.asc())
        )
    )
    return [
        {
            "id": exam.id,
            "title": exam.title,
            "description": exam.description,
            "exam_type": exam.exam_type,
            "status": exam.status,
            "starts_at": exam.starts_at,
            "ends_at": exam.ends_at,
            "duration_minutes": exam.duration_minutes,
            "requires_presence": exam.requires_presence,
            "late_entry_allowed": exam.late_entry_allowed,
            "auto_submit_enabled": exam.auto_submit_enabled,
            "shuffle_questions": exam.shuffle_questions,
            "shuffle_options": exam.shuffle_options,
            "max_attempts": exam.max_attempts,
            "question_count": int(db.scalar(select(func.count(ExamQuestion.id)).where(ExamQuestion.exam_id == exam.id)) or 0),
            "attempt_count": int(db.scalar(select(func.count(ExamSubmission.id)).where(ExamSubmission.exam_id == exam.id)) or 0),
        }
        for exam in exams
    ]


def get_professor_exam_detail(
    *,
    db: Session,
    course_id: int,
    exam_id: int,
) -> dict:
    exam = db.scalar(select(Exam).where(Exam.id == exam_id, Exam.course_id == course_id))
    if exam is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "EXAM_NOT_FOUND",
                "message": "exam not found",
                "details": {"exam_id": exam_id},
            },
        )

    questions = list(
        db.scalars(
            select(ExamQuestion)
            .where(ExamQuestion.exam_id == exam.id)
            .order_by(ExamQuestion.question_order.asc(), ExamQuestion.id.asc())
        )
    )
    option_ids = [question.id for question in questions]
    options = list(
        db.scalars(
            select(ExamQuestionOption)
            .where(ExamQuestionOption.question_id.in_(option_ids))
            .order_by(ExamQuestionOption.question_id.asc(), ExamQuestionOption.option_order.asc(), ExamQuestionOption.id.asc())
        )
    ) if option_ids else []
    option_index: dict[int, list[dict]] = {}
    for option in options:
        option_index.setdefault(option.question_id, []).append(
            {
                "id": option.id,
                "option_order": option.option_order,
                "option_text": option.option_text,
                "is_correct": option.is_correct,
            }
        )
    attachment_index = _exam_question_attachment_index(db, option_ids)

    summary = next(
        exam_summary
        for exam_summary in list_professor_exams(db, course_id)
        if exam_summary["id"] == exam.id
    )
    submission_overview, submissions = _build_professor_submission_snapshot(db=db, exam=exam)
    return {
        **summary,
        "questions": [
            {
                "id": question.id,
                "question_order": question.question_order,
                "question_type": question.question_type,
                "prompt": question.prompt,
                "points": float(question.points),
                "explanation": question.explanation,
                "is_required": question.is_required,
                "selected_option_id": None,
                "options": option_index.get(question.id, []),
                "attachments": attachment_index.get(question.id, []),
            }
            for question in questions
        ],
        "submission_overview": submission_overview,
        "submissions": submissions,
    }


def create_professor_exam(
    *,
    db: Session,
    course_id: int,
    payload: dict,
) -> dict:
    _validate_professor_exam_payload(payload)

    exam = Exam(
        course_id=course_id,
        title=payload["title"].strip(),
        description=(payload.get("description") or "").strip() or None,
        exam_type=payload["exam_type"],
        status="draft",
        starts_at=payload["starts_at"],
        ends_at=payload["ends_at"],
        duration_minutes=payload["duration_minutes"],
        requires_presence=bool(payload.get("requires_presence", True)),
        late_entry_allowed=payload["late_entry_allowed"],
        auto_submit_enabled=payload["auto_submit_enabled"],
        shuffle_questions=payload["shuffle_questions"],
        shuffle_options=payload["shuffle_options"],
        max_attempts=payload["max_attempts"],
    )
    db.add(exam)
    db.flush()
    _replace_professor_exam_questions(db=db, exam_id=exam.id, questions=payload.get("questions") or [])
    db.commit()
    db.refresh(exam)
    return get_professor_exam_detail(db=db, course_id=course_id, exam_id=exam.id)


def update_professor_exam(
    *,
    db: Session,
    course_id: int,
    exam_id: int,
    payload: dict,
) -> dict:
    exam = db.scalar(select(Exam).where(Exam.id == exam_id, Exam.course_id == course_id))
    if exam is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "EXAM_NOT_FOUND",
                "message": "exam not found",
                "details": {"exam_id": exam_id},
            },
        )
    if exam.status != "draft":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "EXAM_NOT_EDITABLE",
                "message": "only draft exams can be updated",
                "details": {"exam_id": exam.id, "status": exam.status},
            },
        )

    _validate_professor_exam_payload(payload)
    exam.title = payload["title"].strip()
    exam.description = (payload.get("description") or "").strip() or None
    exam.exam_type = payload["exam_type"]
    exam.starts_at = payload["starts_at"]
    exam.ends_at = payload["ends_at"]
    exam.duration_minutes = payload["duration_minutes"]
    exam.requires_presence = bool(payload.get("requires_presence", True))
    exam.late_entry_allowed = payload["late_entry_allowed"]
    exam.auto_submit_enabled = payload["auto_submit_enabled"]
    exam.shuffle_questions = payload["shuffle_questions"]
    exam.shuffle_options = payload["shuffle_options"]
    exam.max_attempts = payload["max_attempts"]
    _replace_professor_exam_questions(db=db, exam_id=exam.id, questions=payload.get("questions") or [])
    db.commit()
    db.refresh(exam)
    return get_professor_exam_detail(db=db, course_id=course_id, exam_id=exam.id)


def _process_object_deletion_jobs_if_available(db: Session) -> dict:
    from app.assignments import process_object_deletion_jobs

    return process_object_deletion_jobs(db)


def delete_professor_exam(
    *,
    db: Session,
    course_id: int,
    exam_id: int,
) -> None:
    exam = db.scalar(select(Exam).where(Exam.id == exam_id, Exam.course_id == course_id))
    if exam is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "EXAM_NOT_FOUND",
                "message": "exam not found",
                "details": {"exam_id": exam_id},
            },
        )
    _delete_professor_exam_graph(db=db, exam_id=exam.id)
    db.delete(exam)
    db.commit()
    _process_object_deletion_jobs_if_available(db)


def publish_professor_exam(
    *,
    db: Session,
    course_id: int,
    exam_id: int,
) -> dict:
    exam = db.scalar(select(Exam).where(Exam.id == exam_id, Exam.course_id == course_id))
    if exam is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "EXAM_NOT_FOUND",
                "message": "exam not found",
                "details": {"exam_id": exam_id},
            },
        )
    if exam.status != "draft":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "EXAM_NOT_EDITABLE",
                "message": "only draft exams can be published",
                "details": {"exam_id": exam.id, "status": exam.status},
            },
        )
    exam.status = "published"
    db.commit()
    db.refresh(exam)
    return get_professor_exam_detail(db=db, course_id=course_id, exam_id=exam.id)


def upload_exam_question_attachments(
    db: Session,
    *,
    course_id: int,
    exam_id: int,
    question_id: int,
    files: list[UploadFile],
) -> list[dict]:
    question = db.scalar(
        select(ExamQuestion)
        .join(Exam, Exam.id == ExamQuestion.exam_id)
        .where(Exam.id == exam_id, Exam.course_id == course_id, ExamQuestion.id == question_id)
    )
    if question is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "EXAM_NOT_FOUND", "message": "question not found in this exam", "details": {"question_id": question_id}},
        )
    written_files: list[dict] = []
    try:
        for upload in [item for item in files if item.filename]:
            written_files.append(_store_domain_upload(upload, prefix=f"exams/{exam_id}/questions/{question_id}"))
        attachments: list[ExamQuestionAttachment] = []
        for written in written_files:
            attachment = ExamQuestionAttachment(question_id=question.id, attachment_role="prompt", **written)
            db.add(attachment)
            attachments.append(attachment)
        db.commit()
        for attachment in attachments:
            db.refresh(attachment)
        return [_attachment_payload(attachment, purpose=attachment.attachment_role, question_id=attachment.question_id) for attachment in attachments]
    except Exception:
        db.rollback()
        for written in written_files:
            get_storage_backend_for_metadata(written["storage_provider"], written["bucket_name"]).delete_object(written["storage_key"])
        raise


def get_exam_question_attachment_download(
    db: Session,
    *,
    course_id: int,
    exam_id: int,
    question_id: int,
    attachment_id: int,
) -> ObjectDownload:
    attachment = db.scalar(
        select(ExamQuestionAttachment)
        .join(ExamQuestion, ExamQuestion.id == ExamQuestionAttachment.question_id)
        .join(Exam, Exam.id == ExamQuestion.exam_id)
        .where(
            Exam.id == exam_id,
            Exam.course_id == course_id,
            ExamQuestion.id == question_id,
            ExamQuestionAttachment.id == attachment_id,
        )
    )
    if attachment is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "EXAM_ATTACHMENT_NOT_FOUND", "message": "exam attachment not found", "details": {"attachment_id": attachment_id}},
        )
    return ObjectDownload(
        storage_key=attachment.storage_key,
        filename=attachment.original_filename,
        media_type=attachment.mime_type,
        file_size_bytes=attachment.file_size_bytes,
        storage_provider=attachment.storage_provider,
        bucket_name=attachment.bucket_name,
    )


def _finalize_exam_submission(
    *,
    db: Session,
    exam: Exam,
    submission: ExamSubmission,
    final_status: str,
    reject_missing_required: bool,
    payload_answers: dict[int, dict] | None = None,
) -> dict:
    questions = list(
        db.scalars(
            select(ExamQuestion)
            .where(ExamQuestion.exam_id == exam.id)
            .order_by(ExamQuestion.question_order.asc(), ExamQuestion.id.asc())
        )
    )
    question_index = {question.id: question for question in questions}
    options = list(
        db.scalars(
            select(ExamQuestionOption)
            .where(ExamQuestionOption.question_id.in_(list(question_index)))
            .order_by(ExamQuestionOption.question_id.asc(), ExamQuestionOption.option_order.asc(), ExamQuestionOption.id.asc())
        )
    ) if question_index else []
    options_by_question: dict[int, dict[int, ExamQuestionOption]] = {}
    for option in options:
        options_by_question.setdefault(option.question_id, {})[option.id] = option

    if payload_answers is None:
        saved_answers = list(
            db.scalars(
                select(ExamSubmissionAnswer).where(ExamSubmissionAnswer.submission_id == submission.id)
            )
        )
        answer_by_question = {
            answer.question_id: {
                "question_id": answer.question_id,
                "selected_option_id": answer.selected_option_id,
                "answer_text": answer.answer_text,
            }
            for answer in saved_answers
        }
    else:
        answer_by_question = payload_answers

    missing_required = [question.question_order for question in questions if question.is_required and question.id not in answer_by_question]
    if reject_missing_required and missing_required:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "EXAM_INVALID_PAYLOAD",
                "message": "required questions are unanswered",
                "details": {"question_orders": missing_required},
            },
        )

    for answer in db.scalars(select(ExamSubmissionAnswer).where(ExamSubmissionAnswer.submission_id == submission.id)):
        db.delete(answer)
    db.flush()

    total_score = 0.0
    now = datetime.now(UTC)
    for question in questions:
        payload_answer = answer_by_question.get(question.id)
        if payload_answer is None:
            continue
        selected_option_id = payload_answer.get("selected_option_id")
        if selected_option_id is None:
            continue
        selected_option = options_by_question.get(question.id, {}).get(selected_option_id)
        if selected_option is None:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "EXAM_INVALID_PAYLOAD",
                    "message": "selected option does not belong to the question",
                    "details": {"question_id": question.id, "selected_option_id": selected_option_id},
                },
            )
        is_correct = bool(selected_option.is_correct)
        awarded_score = float(question.points) if is_correct else 0.0
        total_score += awarded_score
        db.add(
            ExamSubmissionAnswer(
                exam_id=exam.id,
                submission_id=submission.id,
                question_id=question.id,
                selected_option_id=selected_option_id,
                answer_text=(payload_answer.get("answer_text") or "").strip() or None,
                is_correct=is_correct,
                awarded_score=awarded_score,
                answered_at=now,
            )
        )

    submission.status = final_status
    submission.submitted_at = now
    submission.score = total_score
    db.flush()

    answered_count = int(
        db.scalar(
            select(func.count(ExamSubmissionAnswer.id)).where(
                ExamSubmissionAnswer.submission_id == submission.id,
                or_(
                    ExamSubmissionAnswer.selected_option_id.is_not(None),
                    ExamSubmissionAnswer.answer_text.is_not(None),
                ),
            )
        ) or 0
    )
    return {
        "exam_id": exam.id,
        "attempt": {
            "id": submission.id,
            "attempt_no": submission.attempt_no,
            "status": submission.status,
            "started_at": submission.started_at,
            "submitted_at": submission.submitted_at,
            "expires_at": submission.expires_at,
            "score": float(submission.score) if submission.score is not None else None,
            "total_count": len(questions),
            "answered_count": answered_count,
        },
        "score": float(submission.score) if submission.score is not None else None,
        "total_count": len(questions),
        "answered_count": answered_count,
    }


def close_professor_exam(
    *,
    db: Session,
    course_id: int,
    exam_id: int,
) -> dict:
    exam = db.scalar(select(Exam).where(Exam.id == exam_id, Exam.course_id == course_id))
    if exam is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "EXAM_NOT_FOUND",
                "message": "exam not found",
                "details": {"exam_id": exam_id},
            },
        )
    if exam.auto_submit_enabled:
        active_submissions = list(
            db.scalars(
                select(ExamSubmission).where(
                    ExamSubmission.exam_id == exam.id,
                    ExamSubmission.status == "in_progress",
                )
            )
        )
        for submission in active_submissions:
            _finalize_exam_submission(
                db=db,
                exam=exam,
                submission=submission,
                final_status="auto_submitted",
                reject_missing_required=False,
                payload_answers=None,
            )
    exam.status = "closed"
    db.commit()
    db.refresh(exam)
    return get_professor_exam_detail(db=db, course_id=course_id, exam_id=exam.id)


def start_student_exam(
    *,
    db: Session,
    presence_client: PresenceClient,
    student_id: str,
    student_user_id: int,
    course_code: str,
    course_id: int,
    exam_id: int,
) -> dict:
    student_visible_statuses = ("published", "open", "closed")

    exam = db.scalar(
        select(Exam).where(
            Exam.id == exam_id,
            Exam.course_id == course_id,
            Exam.status.in_(student_visible_statuses),
        )
    )
    if exam is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "EXAM_NOT_FOUND",
                "message": "exam not found",
                "details": {"exam_id": exam_id},
            },
        )

    existing = db.scalar(
        select(ExamSubmission).where(
            ExamSubmission.exam_id == exam.id,
            ExamSubmission.student_user_id == student_user_id,
            ExamSubmission.status == "in_progress",
        )
    )
    if existing is not None:
        return {
            "submission_id": existing.id,
            "attempt_no": existing.attempt_no,
            "status": existing.status,
            "started_at": existing.started_at,
            "expires_at": existing.expires_at,
            "idempotent": True,
        }

    now = datetime.now(UTC)
    if exam.status not in {"published", "open"} or now < exam.starts_at or now >= exam.ends_at:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "EXAM_NOT_OPEN",
                "message": "exam is not open",
                "details": {"exam_id": exam.id, "status": exam.status},
            },
        )

    if not exam.late_entry_allowed and now > exam.starts_at:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "EXAM_LATE_ENTRY_NOT_ALLOWED",
                "message": "late entry is not allowed for this exam",
                "details": {"exam_id": exam.id},
            },
        )

    attempts_used = db.scalar(
        select(func.count(ExamSubmission.id)).where(
            ExamSubmission.exam_id == exam.id,
            ExamSubmission.student_user_id == student_user_id,
        )
    ) or 0
    if int(attempts_used) >= exam.max_attempts:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "EXAM_ATTEMPT_LIMIT_REACHED",
                "message": "exam attempt limit reached",
                "details": {"exam_id": exam.id, "attempts_used": int(attempts_used), "max_attempts": exam.max_attempts},
            },
        )

    if exam.requires_presence:
        eligibility = check_attendance_eligibility(
            db=db,
            presence_client=presence_client,
            student_id=student_id,
            course_id=course_code,
            classroom_id=None,
            purpose="exam",
        )
        if not eligibility["eligible"]:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "PRESENCE_INELIGIBLE",
                    "message": "presence eligibility is required for this exam",
                    "details": {
                        "exam_id": exam.id,
                        "reason_code": eligibility["reason_code"],
                        "evidence": eligibility.get("evidence", {}),
                    },
                },
            )

    expires_at = min(now + timedelta(minutes=exam.duration_minutes), exam.ends_at)
    effective_seconds = max(0.0, (expires_at - now).total_seconds())
    effective_time_limit_minutes = max(1, math.ceil(effective_seconds / 60)) if effective_seconds > 0 else 1
    submission = ExamSubmission(
        exam_id=exam.id,
        student_user_id=student_user_id,
        attempt_no=int(attempts_used) + 1,
        status="in_progress",
        started_at=now,
        expires_at=expires_at,
        time_limit_snapshot_minutes=effective_time_limit_minutes,
    )
    db.add(submission)
    db.commit()
    db.refresh(submission)
    return {
        "submission_id": submission.id,
        "attempt_no": submission.attempt_no,
        "status": submission.status,
        "started_at": submission.started_at,
        "expires_at": submission.expires_at,
        "idempotent": False,
    }


def submit_student_exam(
    *,
    db: Session,
    student_user_id: int,
    course_id: int,
    exam_id: int,
    payload: dict,
) -> dict:
    student_visible_statuses = ("published", "open", "closed")
    exam = db.scalar(
        select(Exam).where(
            Exam.id == exam_id,
            Exam.course_id == course_id,
            Exam.status.in_(student_visible_statuses),
        )
    )
    if exam is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "EXAM_NOT_FOUND",
                "message": "exam not found",
                "details": {"exam_id": exam_id},
            },
        )

    submission = db.scalar(
        select(ExamSubmission).where(
            ExamSubmission.exam_id == exam.id,
            ExamSubmission.student_user_id == student_user_id,
            ExamSubmission.status == "in_progress",
        )
    )
    if submission is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "EXAM_SUBMISSION_NOT_FOUND",
                "message": "active exam submission not found",
                "details": {"exam_id": exam.id},
            },
        )

    answers = payload.get("answers") or []
    answer_by_question = {answer["question_id"]: answer for answer in answers}
    now = datetime.now(UTC)
    if submission.expires_at is not None and now > submission.expires_at and not exam.auto_submit_enabled:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "EXAM_SUBMISSION_ALREADY_FINALIZED",
                "message": "submission can no longer be finalized manually",
                "details": {"submission_id": submission.id, "status": submission.status},
            },
        )

    result = _finalize_exam_submission(
        db=db,
        exam=exam,
        submission=submission,
        final_status="auto_submitted" if submission.expires_at is not None and now > submission.expires_at else "submitted",
        reject_missing_required=not (submission.expires_at is not None and now > submission.expires_at),
        payload_answers=answer_by_question,
    )
    db.commit()
    db.refresh(submission)
    return result


def save_student_exam_answer(
    *,
    db: Session,
    student_user_id: int,
    course_id: int,
    exam_id: int,
    submission_id: int,
    question_id: int,
    payload: dict,
) -> dict:
    student_visible_statuses = ("published", "open", "closed")
    exam = db.scalar(
        select(Exam).where(
            Exam.id == exam_id,
            Exam.course_id == course_id,
            Exam.status.in_(student_visible_statuses),
        )
    )
    if exam is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "EXAM_NOT_FOUND",
                "message": "exam not found",
                "details": {"exam_id": exam_id},
            },
        )

    submission = db.scalar(
        select(ExamSubmission).where(
            ExamSubmission.id == submission_id,
            ExamSubmission.exam_id == exam.id,
            ExamSubmission.student_user_id == student_user_id,
        )
    )
    if submission is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "EXAM_SUBMISSION_NOT_FOUND",
                "message": "exam submission not found",
                "details": {"submission_id": submission_id},
            },
        )
    if submission.status != "in_progress":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "EXAM_SUBMISSION_ALREADY_FINALIZED",
                "message": "submission is already finalized",
                "details": {"submission_id": submission.id, "status": submission.status},
            },
        )
    now = datetime.now(UTC)
    if submission.expires_at is not None and now > submission.expires_at:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "EXAM_SUBMISSION_EXPIRED",
                "message": "submission answer saves are closed after the exam time window expires",
                "details": {"submission_id": submission.id, "expires_at": submission.expires_at.isoformat()},
            },
        )

    question = db.scalar(select(ExamQuestion).where(ExamQuestion.id == question_id, ExamQuestion.exam_id == exam.id))
    if question is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "EXAM_NOT_FOUND",
                "message": "question not found in this exam",
                "details": {"question_id": question_id},
            },
        )

    selected_option_id = payload.get("selected_option_id")
    if selected_option_id is not None:
        selected_option = db.scalar(
            select(ExamQuestionOption).where(
                ExamQuestionOption.id == selected_option_id,
                ExamQuestionOption.question_id == question.id,
            )
        )
        if selected_option is None:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "EXAM_INVALID_PAYLOAD",
                    "message": "selected option does not belong to the question",
                    "details": {"question_id": question.id, "selected_option_id": selected_option_id},
                },
            )

    answer = db.scalar(
        select(ExamSubmissionAnswer).where(
            ExamSubmissionAnswer.submission_id == submission.id,
            ExamSubmissionAnswer.question_id == question.id,
        )
    )
    answer_text = (payload.get("answer_text") or "").strip() or None
    if answer is None:
        answer = ExamSubmissionAnswer(
            exam_id=exam.id,
            submission_id=submission.id,
            question_id=question.id,
            selected_option_id=selected_option_id,
            answer_text=answer_text,
            answered_at=now,
        )
        db.add(answer)
    else:
        answer.selected_option_id = selected_option_id
        answer.answer_text = answer_text
        answer.answered_at = now

    db.commit()
    db.refresh(answer)
    return {
        "submission_id": submission.id,
        "question_id": question.id,
        "selected_option_id": answer.selected_option_id,
        "answer_text": answer.answer_text,
        "answered_at": answer.answered_at,
    }


def list_notices(db: Session, login_id: str) -> list[dict]:
    user = get_user_by_login_id(db, login_id)
    stmt = (
        select(Notice.id, Notice.title, Notice.body, Course.course_code, User.name, Notice.created_at)
        .join(User, User.id == Notice.author_user_id)
        .join(Course, Course.id == Notice.course_id, isouter=True)
        .order_by(Notice.created_at.desc())
    )
    if user.role == "student":
        stmt = stmt.join(CourseEnrollment, CourseEnrollment.course_id == Notice.course_id, isouter=True).where(
            or_(
                Notice.course_id.is_(None),
                and_(
                    CourseEnrollment.student_user_id == user.id,
                    CourseEnrollment.status == "active",
                ),
            )
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

    rows = list(db.execute(stmt))
    notice_ids = [row[0] for row in rows]
    attachment_index: dict[int, list[dict]] = {}
    if notice_ids:
        attachments = list(
            db.scalars(
                select(NoticeAttachment)
                .where(NoticeAttachment.notice_id.in_(notice_ids))
                .order_by(NoticeAttachment.notice_id.asc(), NoticeAttachment.created_at.asc(), NoticeAttachment.id.asc())
            )
        )
        for attachment in attachments:
            attachment_index.setdefault(attachment.notice_id, []).append(_attachment_payload(attachment))
    return [
        {
            "id": row[0],
            "title": row[1],
            "body": row[2],
            "course_code": row[3],
            "author_name": row[4],
            "created_at": row[5],
            "attachments": attachment_index.get(row[0], []),
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


def create_notice(
    db: Session,
    professor_id: str,
    title: str,
    body: str,
    course_code: str | None,
    *,
    commit: bool = True,
) -> Notice:
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
    if commit:
        db.commit()
        db.refresh(notice)
    else:
        db.flush()
    return notice


def create_notice_with_attachments(
    db: Session,
    professor_id: str,
    title: str,
    body: str,
    course_code: str | None,
    files: list[UploadFile],
) -> Notice:
    written_files: list[dict] = []
    try:
        notice = create_notice(db, professor_id, title, body, course_code, commit=False)
        for upload in [item for item in files if item.filename]:
            written_files.append(_store_domain_upload(upload, prefix=f"notices/{notice.id}"))
        for written in written_files:
            db.add(NoticeAttachment(notice_id=notice.id, **written))
        db.commit()
        db.refresh(notice)
        return notice
    except Exception:
        db.rollback()
        for written in written_files:
            get_storage_backend_for_metadata(written["storage_provider"], written["bucket_name"]).delete_object(written["storage_key"])
        raise


def get_notice_attachment_download(db: Session, login_id: str, notice_id: int, attachment_id: int) -> ObjectDownload:
    get_notice_detail(db, login_id, notice_id)
    attachment = db.scalar(
        select(NoticeAttachment).where(
            NoticeAttachment.id == attachment_id,
            NoticeAttachment.notice_id == notice_id,
        )
    )
    if attachment is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "NOTICE_ATTACHMENT_NOT_FOUND", "message": "notice attachment not found", "details": {"attachment_id": attachment_id}},
        )
    return ObjectDownload(
        storage_key=attachment.storage_key,
        filename=attachment.original_filename,
        media_type=attachment.mime_type,
        file_size_bytes=attachment.file_size_bytes,
        storage_provider=attachment.storage_provider,
        bucket_name=attachment.bucket_name,
    )


def list_learning_items_for_course(db: Session, *, course: Course, include_unpublished: bool = False) -> list[dict]:
    stmt = (
        select(LearningItem, User.name)
        .join(User, User.id == LearningItem.created_by_user_id)
        .where(LearningItem.course_id == course.id)
        .order_by(LearningItem.sort_order.asc(), LearningItem.created_at.desc(), LearningItem.id.desc())
    )
    if not include_unpublished:
        stmt = stmt.where(LearningItem.is_published.is_(True))
    rows = list(db.execute(stmt))
    item_ids = [row[0].id for row in rows]
    attachment_index: dict[int, list[dict]] = {}
    if item_ids:
        attachments = list(
            db.scalars(
                select(LearningItemAttachment)
                .where(LearningItemAttachment.learning_item_id.in_(item_ids))
                .order_by(LearningItemAttachment.learning_item_id.asc(), LearningItemAttachment.created_at.asc(), LearningItemAttachment.id.asc())
            )
        )
        for attachment in attachments:
            item_kind = next((item.item_type for item, _ in rows if item.id == attachment.learning_item_id), None)
            attachment_index.setdefault(attachment.learning_item_id, []).append(_attachment_payload(attachment, purpose=item_kind))
    return [
        {
            "id": item.id,
            "course_code": course.course_code,
            "kind": "video" if item.item_type == "video" else "material",
            "title": item.title,
            "description": item.description,
            "week_label": None,
            "format_label": item.item_type,
            "author_name": author_name,
            "created_at": item.created_at,
            "updated_at": item.updated_at,
            "attachments": attachment_index.get(item.id, []),
            "duration_label": None,
        }
        for item, author_name in rows
    ]


def create_learning_item(
    db: Session,
    *,
    course: Course,
    professor: User,
    kind: str,
    title: str,
    description: str | None,
    files: list[UploadFile],
) -> dict:
    item_kind = "video" if kind == "video" else "document"
    item = LearningItem(
        course_id=course.id,
        created_by_user_id=professor.id,
        title=title.strip(),
        description=(description or "").strip() or None,
        item_type=item_kind,
        is_published=True,
    )
    db.add(item)
    db.flush()
    written_files: list[dict] = []
    try:
        for upload in [item for item in files if item.filename]:
            written_files.append(_store_domain_upload(upload, prefix=f"learning/{course.course_code}/{item.id}"))
        for written in written_files:
            db.add(LearningItemAttachment(learning_item_id=item.id, **written))
        db.commit()
        return next(entry for entry in list_learning_items_for_course(db, course=course, include_unpublished=True) if entry["id"] == item.id)
    except Exception:
        db.rollback()
        for written in written_files:
            get_storage_backend_for_metadata(written["storage_provider"], written["bucket_name"]).delete_object(written["storage_key"])
        raise


def delete_learning_item(db: Session, *, course_id: int, learning_item_id: int) -> None:
    item = db.scalar(select(LearningItem).where(LearningItem.id == learning_item_id, LearningItem.course_id == course_id))
    if item is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "LEARNING_ITEM_NOT_FOUND", "message": "learning item not found", "details": {"learning_item_id": learning_item_id}},
        )
    db.delete(item)
    db.commit()
    _process_object_deletion_jobs_if_available(db)


def get_learning_attachment_download(db: Session, *, course: Course, learning_item_id: int, attachment_id: int) -> ObjectDownload:
    item = db.scalar(select(LearningItem).where(LearningItem.id == learning_item_id, LearningItem.course_id == course.id))
    if item is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "LEARNING_ITEM_NOT_FOUND", "message": "learning item not found", "details": {"learning_item_id": learning_item_id}},
        )
    attachment = db.scalar(
        select(LearningItemAttachment).where(
            LearningItemAttachment.id == attachment_id,
            LearningItemAttachment.learning_item_id == item.id,
        )
    )
    if attachment is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "LEARNING_ATTACHMENT_NOT_FOUND", "message": "learning item attachment not found", "details": {"attachment_id": attachment_id}},
        )
    return ObjectDownload(
        storage_key=attachment.storage_key,
        filename=attachment.original_filename,
        media_type=attachment.mime_type,
        file_size_bytes=attachment.file_size_bytes,
        storage_provider=attachment.storage_provider,
        bucket_name=attachment.bucket_name,
    )


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


ATTENDANCE_CSV_EXPORT_TYPES = {
    "attendance_csv": "summary",
    "attendance_summary_csv": "summary",
    "attendance_full_csv": "full",
}


def _attendance_csv_content(headers: list[str], rows: list[list[str | int]]) -> bytes:
    buffer = StringIO()
    buffer.write("\ufeff")
    writer = csv.writer(buffer)
    writer.writerow(headers)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def create_attendance_csv_export(db: Session, *, professor_id: str, course_code: str, export_type: str = "attendance_summary_csv") -> dict:
    professor = db.scalar(select(User).where(User.professor_id == professor_id, User.role == "professor"))
    course = db.scalar(select(Course).where(Course.course_code == course_code, Course.professor_user_id == getattr(professor, "id", None)))
    if professor is None or course is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "COURSE_NOT_FOUND", "message": "course not found", "details": {"course_code": course_code}},
        )
    variant = ATTENDANCE_CSV_EXPORT_TYPES.get(export_type)
    if variant is None:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "INVALID_REPORT_EXPORT_TYPE",
                "message": "invalid attendance report export type",
                "details": {"export_type": export_type},
            },
        )
    generated_at = datetime.now(UTC)
    csv_table = build_professor_attendance_csv_table(db, professor_id, course_code, variant=variant)
    filename = f"attendance-{variant}-{course_code}-{generated_at.strftime('%Y%m%d%H%M%S')}.csv"
    written = _store_bytes_object(
        content=_attendance_csv_content(csv_table["headers"], csv_table["rows"]),
        filename=filename,
        mime_type="text/csv; charset=utf-8",
        prefix=f"reports/attendance/{course_code}/{generated_at:%Y/%m}",
    )
    report = ReportExport(
        course_id=course.id,
        requested_by_user_id=professor.id,
        report_domain="attendance",
        export_format="csv",
        status="ready",
        generated_at=generated_at,
        **written,
    )
    db.add(report)
    db.commit()
    db.refresh(report)
    return _report_export_payload(report, course.course_code)


def _report_export_payload(report: ReportExport, course_code: str) -> dict:
    return {
        "id": report.id,
        "original_filename": report.original_filename,
        "mime_type": report.mime_type,
        "file_size_bytes": report.file_size_bytes,
        "uploaded_at": report.created_at,
        "storage_provider": report.storage_provider,
        "bucket_name": report.bucket_name,
        "export_type": f"{report.report_domain}_{report.export_format}",
        "course_code": course_code,
        "status": report.status,
        "generated_at": report.generated_at,
    }


def list_attendance_csv_exports(db: Session, *, professor_id: str, course_code: str) -> list[dict]:
    professor = db.scalar(select(User).where(User.professor_id == professor_id, User.role == "professor"))
    course = db.scalar(select(Course).where(Course.course_code == course_code, Course.professor_user_id == getattr(professor, "id", None)))
    if professor is None or course is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "COURSE_NOT_FOUND", "message": "course not found", "details": {"course_code": course_code}},
        )
    reports = list(
        db.scalars(
            select(ReportExport)
            .where(ReportExport.course_id == course.id, ReportExport.report_domain == "attendance")
            .order_by(ReportExport.created_at.desc(), ReportExport.id.desc())
        )
    )
    return [_report_export_payload(report, course.course_code) for report in reports]


def get_report_export_download(db: Session, *, professor_id: str, course_code: str, export_id: int) -> ObjectDownload:
    professor = db.scalar(select(User).where(User.professor_id == professor_id, User.role == "professor"))
    course = db.scalar(select(Course).where(Course.course_code == course_code, Course.professor_user_id == getattr(professor, "id", None)))
    if professor is None or course is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "COURSE_NOT_FOUND", "message": "course not found", "details": {"course_code": course_code}},
        )
    report = db.scalar(select(ReportExport).where(ReportExport.id == export_id, ReportExport.course_id == course.id))
    if report is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "REPORT_EXPORT_NOT_FOUND", "message": "report export not found", "details": {"export_id": export_id}},
        )
    return ObjectDownload(
        storage_key=report.storage_key,
        filename=report.original_filename,
        media_type=report.mime_type,
        file_size_bytes=report.file_size_bytes,
        storage_provider=report.storage_provider,
        bucket_name=report.bucket_name,
    )


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


def _active_schedule_window_filter(weekday: int, current_time):
    previous_weekday = (weekday - 1) % 7
    regular_same_day = and_(
        CourseSchedule.day_of_week == weekday,
        CourseSchedule.starts_at <= CourseSchedule.ends_at,
        CourseSchedule.starts_at <= current_time,
        CourseSchedule.ends_at >= current_time,
    )
    overnight_same_day = and_(
        CourseSchedule.day_of_week == weekday,
        CourseSchedule.starts_at > CourseSchedule.ends_at,
        CourseSchedule.starts_at <= current_time,
    )
    overnight_previous_day = and_(
        CourseSchedule.day_of_week == previous_weekday,
        CourseSchedule.starts_at > CourseSchedule.ends_at,
        CourseSchedule.ends_at >= current_time,
    )
    return or_(regular_same_day, overnight_same_day, overnight_previous_day)


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
            _active_schedule_window_filter(weekday, current_time),
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


def resolve_mapped_classroom_for_course(db: Session, course_id: str) -> str:
    rows = db.execute(
        select(Classroom.classroom_code)
        .select_from(CourseSchedule)
        .join(Course, Course.id == CourseSchedule.course_id)
        .join(Classroom, Classroom.id == CourseSchedule.classroom_id)
        .where(Course.course_code == course_id)
    )
    classroom_codes = {row[0] for row in rows}
    if not classroom_codes:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "CLASSROOM_NOT_MAPPED",
                "message": "no classroom mapping exists for the current course",
                "details": {"course_code": course_id},
            },
        )
    if len(classroom_codes) == 1:
        return classroom_codes.pop()
    raise HTTPException(
        status_code=409,
        detail={
            "code": "CLASSROOM_CONFLICT",
            "message": "multiple classrooms were resolved for the current course",
            "details": {"course_code": course_id, "classroom_codes": sorted(classroom_codes)},
        },
    )


def list_presence_device_options(db: Session, classroom_code: str) -> list[dict]:
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
            RegisteredDevice.status == "active",
            CourseEnrollment.status == "active",
            Classroom.classroom_code == classroom_code,
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


def _parse_observed_at(value) -> datetime | None:
    if value is None or isinstance(value, datetime_module.datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime_module.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def persist_presence_eligibility_log(
    *,
    db: Session,
    student_id: str,
    course_id: str,
    classroom_code: str | None,
    purpose: str,
    result: dict,
    commit: bool = True,
) -> None:
    student = db.scalar(select(User).where(User.student_id == student_id, User.role == "student"))
    course = db.scalar(select(Course).where(Course.course_code == course_id))
    classroom = None
    if classroom_code:
        classroom = db.scalar(select(Classroom).where(Classroom.classroom_code == classroom_code))
    log = PresenceEligibilityLog(
        student_user_id=student.id if student else None,
        course_id=course.id if course else None,
        classroom_id=classroom.id if classroom else None,
        purpose=purpose,
        eligible=bool(result.get("eligible")),
        reason_code=result.get("reason_code") or result.get("reasonCode") or "UNKNOWN",
        matched_device_mac=result.get("matched_device_mac") or result.get("matchedDeviceMac"),
        evidence=result.get("evidence") or {},
        observed_at=_parse_observed_at(result.get("observed_at") or result.get("observedAt")),
        snapshot_age_seconds=result.get("snapshot_age_seconds", result.get("snapshotAgeSeconds")),
    )
    db.add(log)
    if commit:
        db.commit()
    else:
        db.flush()


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

    devices = [device for device in list_devices(db, student_id) if device.status == "active"]
    registered_devices = [{"mac": device.mac_address, "label": device.label} for device in devices]
    if not registered_devices:
        result = {
            "eligible": False,
            "reason_code": "DEVICE_NOT_REGISTERED",
            "matched_device_mac": None,
            "observed_at": None,
            "snapshot_age_seconds": None,
            "evidence": {},
        }
        persist_presence_eligibility_log(
            db=db,
            student_id=student_id,
            course_id=course_id,
            classroom_code=classroom_id,
            purpose=purpose,
            result=result,
        )
        return result

    try:
        resolved_classroom_id = resolve_mapped_classroom_for_course(db, course_id)
    except HTTPException as exc:
        result = {
            "eligible": False,
            "reason_code": exc.detail["code"] if isinstance(exc.detail, dict) else "CLASSROOM_NOT_MAPPED",
            "matched_device_mac": None,
            "observed_at": None,
            "snapshot_age_seconds": None,
            "evidence": exc.detail.get("details", {}) if isinstance(exc.detail, dict) else {},
        }
        persist_presence_eligibility_log(
            db=db,
            student_id=student_id,
            course_id=course_id,
            classroom_code=classroom_id,
            purpose=purpose,
            result=result,
        )
        return result

    classroom_networks = [
        {
            "apId": network["ap_id"],
            "ssid": network["ssid"],
            "signalThresholdDbm": network["signal_threshold_dbm"],
        }
        for network in list_classroom_networks_for_classroom(db, resolved_classroom_id)
    ]
    # Release the read-only DB transaction before the network call to
    # PresenceService. If PresenceService is slow or unavailable, the backend
    # must not pin one SQLAlchemy pool connection per waiting request.
    db.rollback()

    try:
        presence_payload = presence_client.check_eligibility(
            student_id=student_id,
            course_id=course_id,
            classroom_id=resolved_classroom_id,
            purpose=purpose,
            classroom_networks=classroom_networks,
            registered_devices=registered_devices,
            source=settings.presence_eligibility_source,
        )
    except HTTPException as exc:
        if not is_presence_dependency_unavailable(exc):
            raise
        result = presence_dependency_unavailable_result(exc, classroom_id=resolved_classroom_id)
        persist_presence_eligibility_log(
            db=db,
            student_id=student_id,
            course_id=course_id,
            classroom_code=resolved_classroom_id,
            purpose=purpose,
            result=result,
        )
        return result

    result = {
        "eligible": bool(presence_payload.get("eligible")),
        "reason_code": presence_payload.get("reasonCode", "UNKNOWN"),
        "matched_device_mac": presence_payload.get("matchedDeviceMac"),
        "observed_at": presence_payload.get("observedAt"),
        "snapshot_age_seconds": presence_payload.get("snapshotAgeSeconds"),
        "evidence": presence_payload.get("evidence", {}),
    }
    persist_presence_eligibility_log(
        db=db,
        student_id=student_id,
        course_id=course_id,
        classroom_code=resolved_classroom_id,
        purpose=purpose,
        result=result,
    )
    return result
