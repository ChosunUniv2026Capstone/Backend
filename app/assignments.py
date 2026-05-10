from dataclasses import dataclass
from datetime import UTC, datetime
import os
import re
from uuid import uuid4

from fastapi import HTTPException, UploadFile
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Assignment, AssignmentSubmission, AssignmentSubmissionAttachment, CourseEnrollment, User
from app.storage import ObjectNotFoundError, get_storage_backend, get_storage_backend_for_metadata, spool_limited_upload

settings = get_settings()
ASSIGNMENT_STORAGE_PREFIX = "assignments"
FILENAME_SANITIZE_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")
MAX_ASSIGNMENT_TITLE_LENGTH = 200
MAX_ORIGINAL_FILENAME_LENGTH = 255


def _assignment_status(assignment: Assignment, now: datetime | None = None) -> str:
    current = now or datetime.now(UTC)
    if current < assignment.opens_at:
        return "upcoming"
    if current > assignment.due_at:
        return "closed"
    return "open"


def _assignment_not_found_error(assignment_id: int) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={
            "code": "ASSIGNMENT_NOT_FOUND",
            "message": "assignment not found",
            "details": {"assignment_id": assignment_id},
        },
    )


def _attachment_not_found_error(attachment_id: int) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={
            "code": "ASSIGNMENT_ATTACHMENT_NOT_FOUND",
            "message": "assignment attachment not found",
            "details": {"attachment_id": attachment_id},
        },
    )


def _load_assignment(db: Session, *, course_id: int, assignment_id: int) -> Assignment:
    assignment = db.scalar(
        select(Assignment).where(
            Assignment.id == assignment_id,
            Assignment.course_id == course_id,
        )
    )
    if assignment is None:
        raise _assignment_not_found_error(assignment_id)
    return assignment


def _invalid_payload_error(message: str, *, field: str, **details: object) -> HTTPException:
    return HTTPException(
        status_code=400,
        detail={
            "code": "ASSIGNMENT_INVALID_PAYLOAD",
            "message": message,
            "details": {"field": field, **details},
        },
    )


def _normalize_title(value: str) -> str:
    title = value.strip()
    if not title:
        raise _invalid_payload_error("assignment title is required", field="title")
    if len(title) > MAX_ASSIGNMENT_TITLE_LENGTH:
        raise _invalid_payload_error(
            "assignment title must be 200 characters or fewer",
            field="title",
            max_length=MAX_ASSIGNMENT_TITLE_LENGTH,
        )
    return title


def _validate_assignment_window(opens_at: datetime, due_at: datetime) -> None:
    if due_at <= opens_at:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "ASSIGNMENT_INVALID_WINDOW",
                "message": "assignment due_at must be later than opens_at",
                "details": {},
            },
        )


def _normalize_original_filename(filename: str | None) -> str:
    candidate = (filename or "attachment").replace("\x00", "").strip()
    if not candidate:
        candidate = "attachment"
    return candidate.replace("/", "_").replace("\\", "_")[:MAX_ORIGINAL_FILENAME_LENGTH] or "attachment"


def _sanitize_filename(filename: str | None) -> str:
    candidate = _normalize_original_filename(filename)
    sanitized = FILENAME_SANITIZE_PATTERN.sub("_", candidate)
    return sanitized[:180] or "attachment"


@dataclass(frozen=True)
class AssignmentAttachmentDownload:
    storage_key: str
    filename: str
    media_type: str | None
    file_size_bytes: int
    storage_provider: str | None = None
    bucket_name: str | None = None


def _assignment_storage_key(*, assignment_id: int, submission_id: int, student_user_id: int, stored_filename: str) -> str:
    return os.path.join(
        ASSIGNMENT_STORAGE_PREFIX,
        str(assignment_id),
        "submissions",
        str(submission_id),
        "students",
        str(student_user_id),
        stored_filename,
    )


def _current_storage_metadata() -> tuple[str, str]:
    backend = get_storage_backend()
    provider = backend.provider if backend.provider in {"local", "s3"} else "local"
    bucket_name = backend.bucket_name or ("local" if provider == "local" else settings.object_storage_bucket)
    return provider, bucket_name


def _storage_backend_for_metadata(provider: str | None, bucket_name: str | None):
    return get_storage_backend_for_metadata(provider or settings.object_storage_provider, bucket_name or settings.object_storage_bucket)


def _storage_backend_for_attachment(attachment: AssignmentSubmissionAttachment):
    return _storage_backend_for_metadata(
        getattr(attachment, "storage_provider", None),
        getattr(attachment, "bucket_name", None),
    )


def _serialize_attachment(attachment: AssignmentSubmissionAttachment) -> dict:
    return {
        "id": attachment.id,
        "original_filename": attachment.original_filename,
        "mime_type": attachment.mime_type,
        "file_size_bytes": attachment.file_size_bytes,
        "uploaded_at": attachment.created_at,
    }


def _load_attachment_index(db: Session, submission_ids: list[int]) -> dict[int, list[AssignmentSubmissionAttachment]]:
    if not submission_ids:
        return {}

    attachments = list(
        db.scalars(
            select(AssignmentSubmissionAttachment)
            .where(AssignmentSubmissionAttachment.submission_id.in_(submission_ids))
            .order_by(AssignmentSubmissionAttachment.created_at.asc(), AssignmentSubmissionAttachment.id.asc())
        )
    )
    index: dict[int, list[AssignmentSubmissionAttachment]] = {}
    for attachment in attachments:
        index.setdefault(attachment.submission_id, []).append(attachment)
    return index


def _serialize_student_submission(
    submission: AssignmentSubmission,
    attachments: list[AssignmentSubmissionAttachment],
) -> dict:
    return {
        "id": submission.id,
        "submission_text": submission.submission_text,
        "submitted_at": submission.submitted_at,
        "updated_at": submission.updated_at,
        "attachments": [_serialize_attachment(attachment) for attachment in attachments],
    }


def _serialize_student_assignment_summary(
    assignment: Assignment,
    submission: AssignmentSubmission | None,
    attachments: list[AssignmentSubmissionAttachment],
) -> dict:
    return {
        "id": assignment.id,
        "title": assignment.title,
        "description": assignment.description,
        "opens_at": assignment.opens_at,
        "due_at": assignment.due_at,
        "status": _assignment_status(assignment),
        "created_at": assignment.created_at,
        "submitted": submission is not None,
        "submitted_at": submission.submitted_at if submission else None,
        "attachment_count": len(attachments),
    }


def _serialize_professor_assignment_summary(
    assignment: Assignment,
    *,
    submission_count: int,
    total_students: int,
) -> dict:
    return {
        "id": assignment.id,
        "title": assignment.title,
        "description": assignment.description,
        "opens_at": assignment.opens_at,
        "due_at": assignment.due_at,
        "status": _assignment_status(assignment),
        "created_at": assignment.created_at,
        "submission_count": submission_count,
        "total_students": total_students,
    }


def list_student_assignments(db: Session, *, student_user_id: int, course_id: int) -> list[dict]:
    assignments = list(
        db.scalars(
            select(Assignment)
            .where(Assignment.course_id == course_id)
            .order_by(Assignment.due_at.desc(), Assignment.id.desc())
        )
    )
    if not assignments:
        return []

    assignment_ids = [assignment.id for assignment in assignments]
    submissions = list(
        db.scalars(
            select(AssignmentSubmission).where(
                AssignmentSubmission.assignment_id.in_(assignment_ids),
                AssignmentSubmission.student_user_id == student_user_id,
            )
        )
    )
    submission_by_assignment = {submission.assignment_id: submission for submission in submissions}
    attachment_index = _load_attachment_index(db, [submission.id for submission in submissions])

    return [
        _serialize_student_assignment_summary(
            assignment,
            submission_by_assignment.get(assignment.id),
            attachment_index.get(submission_by_assignment[assignment.id].id, [])
            if assignment.id in submission_by_assignment
            else [],
        )
        for assignment in assignments
    ]


def get_student_assignment_detail(db: Session, *, student_user_id: int, course_id: int, assignment_id: int) -> dict:
    assignment = _load_assignment(db, course_id=course_id, assignment_id=assignment_id)
    submission = db.scalar(
        select(AssignmentSubmission).where(
            AssignmentSubmission.assignment_id == assignment.id,
            AssignmentSubmission.student_user_id == student_user_id,
        )
    )
    attachments = (
        list(
            db.scalars(
                select(AssignmentSubmissionAttachment)
                .where(AssignmentSubmissionAttachment.submission_id == submission.id)
                .order_by(AssignmentSubmissionAttachment.created_at.asc(), AssignmentSubmissionAttachment.id.asc())
            )
        )
        if submission
        else []
    )

    payload = _serialize_student_assignment_summary(assignment, submission, attachments)
    payload["submission"] = _serialize_student_submission(submission, attachments) if submission else None
    return payload


def list_professor_assignments(db: Session, *, course_id: int) -> list[dict]:
    assignments = list(
        db.scalars(
            select(Assignment)
            .where(Assignment.course_id == course_id)
            .order_by(Assignment.due_at.desc(), Assignment.id.desc())
        )
    )
    if not assignments:
        return []

    assignment_ids = [assignment.id for assignment in assignments]
    rows = db.execute(
        select(AssignmentSubmission.assignment_id, func.count(AssignmentSubmission.id))
        .where(AssignmentSubmission.assignment_id.in_(assignment_ids))
        .group_by(AssignmentSubmission.assignment_id)
    )
    submission_count_index = {int(assignment_id): int(count) for assignment_id, count in rows}
    total_students = int(
        db.scalar(
            select(func.count(CourseEnrollment.id)).where(
                CourseEnrollment.course_id == course_id,
                CourseEnrollment.status == "active",
            )
        )
        or 0
    )

    return [
        _serialize_professor_assignment_summary(
            assignment,
            submission_count=submission_count_index.get(assignment.id, 0),
            total_students=total_students,
        )
        for assignment in assignments
    ]


def get_professor_assignment_detail(db: Session, *, course_id: int, assignment_id: int) -> dict:
    assignment = _load_assignment(db, course_id=course_id, assignment_id=assignment_id)
    submissions = list(
        db.scalars(
            select(AssignmentSubmission)
            .where(AssignmentSubmission.assignment_id == assignment.id)
            .order_by(AssignmentSubmission.submitted_at.desc(), AssignmentSubmission.id.desc())
        )
    )
    attachment_index = _load_attachment_index(db, [submission.id for submission in submissions])
    student_ids = [submission.student_user_id for submission in submissions]
    students = list(db.scalars(select(User).where(User.id.in_(student_ids)))) if student_ids else []
    student_index = {student.id: student for student in students}
    total_students = int(
        db.scalar(
            select(func.count(CourseEnrollment.id)).where(
                CourseEnrollment.course_id == course_id,
                CourseEnrollment.status == "active",
            )
        )
        or 0
    )

    payload = _serialize_professor_assignment_summary(
        assignment,
        submission_count=len(submissions),
        total_students=total_students,
    )
    payload["submissions"] = [
        {
            "id": submission.id,
            "student_id": student_index[submission.student_user_id].student_id if submission.student_user_id in student_index else "",
            "student_name": student_index[submission.student_user_id].name if submission.student_user_id in student_index else "Unknown",
            "submission_text": submission.submission_text,
            "submitted_at": submission.submitted_at,
            "updated_at": submission.updated_at,
            "attachments": [
                _serialize_attachment(attachment)
                for attachment in attachment_index.get(submission.id, [])
            ],
        }
        for submission in submissions
    ]
    return payload


def create_professor_assignment(db: Session, *, course_id: int, payload: dict) -> dict:
    opens_at = payload["opens_at"]
    due_at = payload["due_at"]
    _validate_assignment_window(opens_at, due_at)

    assignment = Assignment(
        course_id=course_id,
        title=_normalize_title(payload["title"]),
        description=(payload.get("description") or "").strip() or None,
        opens_at=opens_at,
        due_at=due_at,
    )
    db.add(assignment)
    db.commit()
    db.refresh(assignment)
    return get_professor_assignment_detail(db, course_id=course_id, assignment_id=assignment.id)


def _store_upload_file(
    upload: UploadFile,
    *,
    assignment_id: int,
    submission_id: int,
    student_user_id: int,
) -> dict:
    original_filename = _normalize_original_filename(upload.filename)
    sanitized_name = _sanitize_filename(original_filename)
    stored_filename = f"{uuid4().hex}_{sanitized_name}"
    storage_key = _assignment_storage_key(
        assignment_id=assignment_id,
        submission_id=submission_id,
        student_user_id=student_user_id,
        stored_filename=stored_filename,
    )
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
                "code": "ASSIGNMENT_SUBMISSION_FILE_TOO_LARGE",
                "message": "assignment submission file is too large",
                "details": {
                    "filename": upload.filename or sanitized_name,
                    "max_file_size_bytes": settings.assignment_upload_max_file_size_bytes,
                },
            },
        ) from exc

    with spooled_file:
        get_storage_backend().put_object(storage_key, spooled_file, content_type=mime_type)

    storage_provider, bucket_name = _current_storage_metadata()
    return {
        "storage_provider": storage_provider,
        "bucket_name": bucket_name,
        "original_filename": original_filename,
        "stored_filename": stored_filename,
        "mime_type": mime_type,
        "file_size_bytes": file_size,
        "storage_key": storage_key,
    }


def submit_student_assignment(
    db: Session,
    *,
    student_user_id: int,
    course_id: int,
    assignment_id: int,
    submission_text: str | None,
    files: list[UploadFile],
) -> dict:
    assignment = _load_assignment(db, course_id=course_id, assignment_id=assignment_id)
    if _assignment_status(assignment) != "open":
        raise HTTPException(
            status_code=400,
            detail={
                "code": "ASSIGNMENT_NOT_OPEN",
                "message": "assignment submission is closed",
                "details": {"assignment_id": assignment_id},
            },
        )

    normalized_files = [upload for upload in files if upload.filename]
    if len(normalized_files) > settings.assignment_upload_max_files:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "ASSIGNMENT_SUBMISSION_FILE_LIMIT_EXCEEDED",
                "message": "too many assignment submission files",
                "details": {"max_files": settings.assignment_upload_max_files},
            },
        )

    normalized_text = (submission_text or "").strip() or None
    submission = db.scalar(
        select(AssignmentSubmission).where(
            AssignmentSubmission.assignment_id == assignment.id,
            AssignmentSubmission.student_user_id == student_user_id,
        )
    )
    if submission is None:
        submission = AssignmentSubmission(
            assignment_id=assignment.id,
            student_user_id=student_user_id,
            submission_text=normalized_text,
        )
        db.add(submission)
        db.flush()

    existing_attachments = list(
        db.scalars(
            select(AssignmentSubmissionAttachment)
            .where(AssignmentSubmissionAttachment.submission_id == submission.id)
            .order_by(AssignmentSubmissionAttachment.id.asc())
        )
    )

    if not normalized_text and not normalized_files and not existing_attachments:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "ASSIGNMENT_INVALID_PAYLOAD",
                "message": "submission text or file is required",
                "details": {},
            },
        )

    written_files: list[dict] = []
    try:
        for upload in normalized_files:
            written_files.append(
                _store_upload_file(
                    upload,
                    assignment_id=assignment.id,
                    submission_id=submission.id,
                    student_user_id=student_user_id,
                )
            )

        submission.submission_text = normalized_text
        submission.submitted_at = datetime.now(UTC)

        if normalized_files:
            for existing in existing_attachments:
                db.delete(existing)

            db.flush()

            for written in written_files:
                db.add(
                    AssignmentSubmissionAttachment(
                        submission_id=submission.id,
                        original_filename=written["original_filename"],
                        stored_filename=written["stored_filename"],
                        mime_type=written["mime_type"],
                        file_size_bytes=written["file_size_bytes"],
                        storage_provider=written["storage_provider"],
                        bucket_name=written["bucket_name"],
                        storage_key=written["storage_key"],
                    )
                )

        db.commit()
    except Exception:
        db.rollback()
        for written in written_files:
            _delete_object_immediately(
                written["storage_key"],
                storage_provider=written.get("storage_provider"),
                bucket_name=written.get("bucket_name"),
            )
        raise

    if normalized_files:
        enqueued_deletions = False
        for existing in existing_attachments:
            enqueued_deletions = _delete_or_enqueue_object(db, existing, reason="assignment_attachment_replaced") or enqueued_deletions
        if enqueued_deletions:
            process_object_deletion_jobs(db)

    return get_student_assignment_detail(
        db,
        student_user_id=student_user_id,
        course_id=course_id,
        assignment_id=assignment.id,
    )


def get_student_assignment_attachment_download(
    db: Session,
    *,
    student_user_id: int,
    course_id: int,
    assignment_id: int,
    attachment_id: int,
) -> AssignmentAttachmentDownload:
    _load_assignment(db, course_id=course_id, assignment_id=assignment_id)
    attachment = db.scalar(
        select(AssignmentSubmissionAttachment)
        .join(AssignmentSubmission, AssignmentSubmission.id == AssignmentSubmissionAttachment.submission_id)
        .where(
            AssignmentSubmissionAttachment.id == attachment_id,
            AssignmentSubmission.assignment_id == assignment_id,
            AssignmentSubmission.student_user_id == student_user_id,
        )
    )
    if attachment is None:
        raise _attachment_not_found_error(attachment_id)

    _ensure_storage_object_exists(attachment, attachment_id=attachment_id)
    return AssignmentAttachmentDownload(
        storage_key=attachment.storage_key,
        filename=attachment.original_filename,
        media_type=attachment.mime_type,
        file_size_bytes=attachment.file_size_bytes,
        storage_provider=getattr(attachment, "storage_provider", None),
        bucket_name=getattr(attachment, "bucket_name", None),
    )


def get_professor_assignment_attachment_download(
    db: Session,
    *,
    course_id: int,
    assignment_id: int,
    attachment_id: int,
) -> AssignmentAttachmentDownload:
    _load_assignment(db, course_id=course_id, assignment_id=assignment_id)
    attachment = db.scalar(
        select(AssignmentSubmissionAttachment)
        .join(AssignmentSubmission, AssignmentSubmission.id == AssignmentSubmissionAttachment.submission_id)
        .where(
            AssignmentSubmissionAttachment.id == attachment_id,
            AssignmentSubmission.assignment_id == assignment_id,
        )
    )
    if attachment is None:
        raise _attachment_not_found_error(attachment_id)

    _ensure_storage_object_exists(attachment, attachment_id=attachment_id)
    return AssignmentAttachmentDownload(
        storage_key=attachment.storage_key,
        filename=attachment.original_filename,
        media_type=attachment.mime_type,
        file_size_bytes=attachment.file_size_bytes,
        storage_provider=getattr(attachment, "storage_provider", None),
        bucket_name=getattr(attachment, "bucket_name", None),
    )


def _ensure_storage_object_exists(attachment: AssignmentSubmissionAttachment, *, attachment_id: int) -> None:
    try:
        _storage_backend_for_attachment(attachment).head_object(attachment.storage_key)
    except ObjectNotFoundError as exc:
        raise _attachment_not_found_error(attachment_id) from exc


def _delete_object_immediately(storage_key: str, *, storage_provider: str | None = None, bucket_name: str | None = None) -> bool:
    try:
        _storage_backend_for_metadata(storage_provider, bucket_name).delete_object(storage_key)
        return True
    except Exception:
        return False


def _delete_or_enqueue_object(db: Session, attachment: AssignmentSubmissionAttachment, *, reason: str) -> bool:
    if _enqueue_object_deletion_job(db, attachment, reason=reason):
        return True
    _delete_object_immediately(
        attachment.storage_key,
        storage_provider=getattr(attachment, "storage_provider", None),
        bucket_name=getattr(attachment, "bucket_name", None),
    )
    return False


def _enqueue_object_deletion_job(db: Session, attachment: AssignmentSubmissionAttachment, *, reason: str) -> bool:
    bind = db.get_bind()
    try:
        if not inspect(bind).has_table("object_deletion_jobs"):
            return False
        storage_provider = getattr(attachment, "storage_provider", None) or _current_storage_metadata()[0]
        bucket_name = getattr(attachment, "bucket_name", None) or _current_storage_metadata()[1]
        db.execute(
            text(
                """
                INSERT INTO object_deletion_jobs
                    (storage_provider, bucket_name, storage_key, owner_domain, owner_id, reason, status, attempt_count)
                SELECT
                    :storage_provider, :bucket_name, :storage_key, :owner_domain, :owner_id, :reason, 'pending', 0
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM object_deletion_jobs
                    WHERE storage_provider = :storage_provider
                      AND bucket_name = :bucket_name
                      AND storage_key = :storage_key
                      AND status IN ('pending', 'processing')
                )
                """
            ),
            {
                "storage_provider": storage_provider,
                "bucket_name": bucket_name,
                "storage_key": attachment.storage_key,
                "owner_domain": "assignment_submission_attachment",
                "owner_id": attachment.id,
                "reason": reason,
            },
        )
        db.commit()
        return True
    except SQLAlchemyError:
        db.rollback()
        return False


def process_object_deletion_jobs(db: Session, *, limit: int = 100) -> dict:
    try:
        if not inspect(db.get_bind()).has_table("object_deletion_jobs"):
            return {"processed": 0, "deleted": 0, "failed": 0, "skipped": True}
        rows = db.execute(
            text(
                """
                SELECT id, storage_provider, bucket_name, storage_key
                FROM object_deletion_jobs
                WHERE status IN ('pending', 'failed')
                ORDER BY created_at ASC, id ASC
                LIMIT :limit
                """
            ),
            {"limit": limit},
        ).mappings().all()
    except SQLAlchemyError:
        db.rollback()
        return {"processed": 0, "deleted": 0, "failed": 0, "skipped": True}

    deleted = 0
    failed = 0
    for row in rows:
        try:
            _storage_backend_for_metadata(row.get("storage_provider"), row.get("bucket_name")).delete_object(row["storage_key"])
            db.execute(
                text(
                    """
                    UPDATE object_deletion_jobs
                    SET status = 'completed', completed_at = CURRENT_TIMESTAMP, last_error = NULL
                    WHERE id = :id
                    """
                ),
                {"id": row["id"]},
            )
            deleted += 1
        except Exception as exc:
            db.execute(
                text(
                    """
                    UPDATE object_deletion_jobs
                    SET status = 'failed', attempt_count = attempt_count + 1, last_error = :last_error
                    WHERE id = :id
                    """
                ),
                {"id": row["id"], "last_error": str(exc)[:500]},
            )
            failed += 1
    db.commit()
    return {"processed": len(rows), "deleted": deleted, "failed": failed, "skipped": False}
