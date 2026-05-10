from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AssignmentSubmissionAttachment
from app.storage import ObjectNotFoundError, get_storage_backend


@dataclass(frozen=True)
class AssignmentStorageImportResult:
    inspected: int
    imported: int
    already_present: int
    missing_local: int


def import_legacy_assignment_files(db: Session, *, dry_run: bool = False) -> AssignmentStorageImportResult:
    """Import legacy Backend-local assignment files into the configured object store.

    Legacy rows already store an internal `storage_key`; this utility copies objects
    from `assignment_upload_dir/<storage_key>` when the configured object backend does
    not already contain the key. It does not mutate DB metadata, so it is safe to run
    repeatedly during a local->Garage/S3 migration window.
    """

    settings = get_settings()
    legacy_root = Path(settings.assignment_upload_dir).resolve()
    storage = get_storage_backend()
    inspected = imported = already_present = missing_local = 0

    attachments = list(
        db.scalars(
            select(AssignmentSubmissionAttachment).order_by(
                AssignmentSubmissionAttachment.created_at.asc(),
                AssignmentSubmissionAttachment.id.asc(),
            )
        )
    )
    for attachment in attachments:
        inspected += 1
        try:
            storage.head_object(attachment.storage_key)
            already_present += 1
            continue
        except ObjectNotFoundError:
            pass

        source_path = (legacy_root / attachment.storage_key).resolve()
        if legacy_root != source_path and legacy_root not in source_path.parents:
            missing_local += 1
            continue
        if not source_path.exists() or not source_path.is_file():
            missing_local += 1
            continue
        if not dry_run:
            with source_path.open("rb") as handle:
                storage.put_object(attachment.storage_key, handle, content_type=attachment.mime_type)
        imported += 1

    return AssignmentStorageImportResult(
        inspected=inspected,
        imported=imported,
        already_present=already_present,
        missing_local=missing_local,
    )
