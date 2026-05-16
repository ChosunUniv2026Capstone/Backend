from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Assignment,
    AssignmentSubmission,
    Course,
    CourseEnrollment,
    CourseQnaPost,
    CourseQnaThread,
    Exam,
    ExamQuestion,
    ExamSubmission,
    LearningItem,
    LearningProgress,
    User,
)

LEARNING_PROGRESS_STATUSES = {"not_started", "in_progress", "completed"}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _api_error(status_code: int, code: str, message: str, details: dict | None = None) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message, "details": details or {}})


def _student_payload(student: User) -> dict:
    return {"student_id": student.student_id or "", "student_name": student.name}


def _score_percent(score: float | None, max_score: float | None) -> float | None:
    if score is None or max_score is None or max_score <= 0:
        return None
    return round((score / max_score) * 100, 2)


def _overall_percent(items: list[dict]) -> float | None:
    percents = [item["percent"] for item in items if item.get("percent") is not None]
    if not percents:
        return None
    return round(sum(percents) / len(percents), 2)


def _assignment_grade_entries(db: Session, *, course_id: int, student_user_id: int) -> list[dict]:
    rows = db.execute(
        select(Assignment, AssignmentSubmission)
        .outerjoin(
            AssignmentSubmission,
            (AssignmentSubmission.assignment_id == Assignment.id)
            & (AssignmentSubmission.student_user_id == student_user_id),
        )
        .where(Assignment.course_id == course_id)
        .order_by(Assignment.due_at.asc(), Assignment.id.asc())
    ).all()
    entries: list[dict] = []
    for assignment, submission in rows:
        score = float(submission.score) if submission and submission.score is not None else None
        max_score = float(assignment.max_score)
        entries.append(
            {
                "type": "assignment",
                "item_type": "assignment",
                "assignment_id": assignment.id,
                "item_id": assignment.id,
                "title": assignment.title,
                "score": score,
                "max_score": max_score,
                "percent": _score_percent(score, max_score),
                "feedback": submission.feedback if submission else None,
                "grading_status": submission.grading_status if submission else None,
                "submitted_at": submission.submitted_at if submission else None,
                "graded_at": submission.graded_at if submission else None,
                "due_at": assignment.due_at,
            }
        )
    return entries


def _exam_max_score_index(db: Session, exam_ids: list[int]) -> dict[int, float]:
    if not exam_ids:
        return {}
    rows = db.execute(
        select(ExamQuestion.exam_id, func.coalesce(func.sum(ExamQuestion.points), 0))
        .where(ExamQuestion.exam_id.in_(exam_ids))
        .group_by(ExamQuestion.exam_id)
    )
    return {int(exam_id): float(total or 0) for exam_id, total in rows}


def _latest_exam_submission_index(db: Session, *, exam_ids: list[int], student_user_id: int) -> dict[int, ExamSubmission]:
    if not exam_ids:
        return {}
    submissions = list(
        db.scalars(
            select(ExamSubmission)
            .where(
                ExamSubmission.exam_id.in_(exam_ids),
                ExamSubmission.student_user_id == student_user_id,
            )
            .order_by(ExamSubmission.exam_id.asc(), ExamSubmission.attempt_no.desc(), ExamSubmission.id.desc())
        )
    )
    latest: dict[int, ExamSubmission] = {}
    for submission in submissions:
        latest.setdefault(submission.exam_id, submission)
    return latest


def _exam_grade_entries(db: Session, *, course_id: int, student_user_id: int) -> list[dict]:
    exams = list(db.scalars(select(Exam).where(Exam.course_id == course_id).order_by(Exam.starts_at.asc(), Exam.id.asc())))
    exam_ids = [exam.id for exam in exams]
    max_score_index = _exam_max_score_index(db, exam_ids)
    latest_index = _latest_exam_submission_index(db, exam_ids=exam_ids, student_user_id=student_user_id)
    entries: list[dict] = []
    for exam in exams:
        submission = latest_index.get(exam.id)
        score = float(submission.score) if submission and submission.score is not None else None
        max_score = max_score_index.get(exam.id)
        entries.append(
            {
                "type": "exam",
                "item_type": "exam",
                "exam_id": exam.id,
                "item_id": exam.id,
                "title": exam.title,
                "score": score,
                "max_score": max_score,
                "percent": _score_percent(score, max_score),
                "status": submission.status if submission else None,
                "submitted_at": submission.submitted_at if submission else None,
                "due_at": exam.ends_at,
            }
        )
    return entries


def build_student_grades(db: Session, *, student: User, course: Course) -> dict:
    assignments = _assignment_grade_entries(db, course_id=course.id, student_user_id=student.id)
    exams = _exam_grade_entries(db, course_id=course.id, student_user_id=student.id)
    items = assignments + exams
    return {
        "course_code": course.course_code,
        **_student_payload(student),
        "overall_percent": _overall_percent(items),
        "items": items,
        "assignments": assignments,
        "exams": exams,
    }


def build_professor_grades(db: Session, *, course: Course) -> list[dict]:
    students = list(
        db.scalars(
            select(User)
            .join(CourseEnrollment, CourseEnrollment.student_user_id == User.id)
            .where(CourseEnrollment.course_id == course.id, CourseEnrollment.status == "active")
            .order_by(User.student_id.asc(), User.id.asc())
        )
    )
    return [build_student_grades(db, student=student, course=course) for student in students]


def _serialize_qna_post(post: CourseQnaPost, author: User) -> dict:
    return {
        "id": post.id,
        "author_id": author.student_id or author.professor_id or author.admin_id or str(author.id),
        "author_name": author.name,
        "author_role": author.role,
        "body": post.body,
        "post_type": post.post_type,
        "created_at": post.created_at,
    }


def _load_qna_posts(db: Session, thread_ids: list[int]) -> dict[int, list[dict]]:
    if not thread_ids:
        return {}
    rows = db.execute(
        select(CourseQnaPost, User)
        .join(User, User.id == CourseQnaPost.author_user_id)
        .where(CourseQnaPost.thread_id.in_(thread_ids))
        .order_by(CourseQnaPost.created_at.asc(), CourseQnaPost.id.asc())
    ).all()
    index: dict[int, list[dict]] = {}
    for post, author in rows:
        index.setdefault(post.thread_id, []).append(_serialize_qna_post(post, author))
    return index


def _serialize_qna_thread(thread: CourseQnaThread, student: User, posts: list[dict]) -> dict:
    return {
        "id": thread.id,
        "student_id": student.student_id or "",
        "student_name": student.name,
        "title": thread.title,
        "body": thread.body,
        "status": thread.status,
        "created_at": thread.created_at,
        "updated_at": thread.updated_at,
        "posts": posts,
    }


def list_student_qna_threads(db: Session, *, course: Course, student: User) -> list[dict]:
    threads = list(
        db.scalars(
            select(CourseQnaThread)
            .where(CourseQnaThread.course_id == course.id, CourseQnaThread.student_user_id == student.id)
            .order_by(CourseQnaThread.updated_at.desc(), CourseQnaThread.id.desc())
        )
    )
    posts = _load_qna_posts(db, [thread.id for thread in threads])
    return [_serialize_qna_thread(thread, student, posts.get(thread.id, [])) for thread in threads]


def list_professor_qna_threads(db: Session, *, course: Course) -> list[dict]:
    rows = list(
        db.execute(
            select(CourseQnaThread, User)
            .join(User, User.id == CourseQnaThread.student_user_id)
            .where(CourseQnaThread.course_id == course.id)
            .order_by(CourseQnaThread.updated_at.desc(), CourseQnaThread.id.desc())
        )
    )
    posts = _load_qna_posts(db, [thread.id for thread, _student in rows])
    return [_serialize_qna_thread(thread, student, posts.get(thread.id, [])) for thread, student in rows]


def create_student_qna_thread(db: Session, *, course: Course, student: User, title: str, body: str) -> dict:
    normalized_title = title.strip()
    normalized_body = body.strip()
    if not normalized_title or not normalized_body:
        raise _api_error(400, "QNA_INVALID_PAYLOAD", "qna title and body are required")
    thread = CourseQnaThread(
        course_id=course.id,
        student_user_id=student.id,
        title=normalized_title[:200],
        body=normalized_body,
        status="open",
    )
    db.add(thread)
    db.flush()
    db.add(CourseQnaPost(thread_id=thread.id, author_user_id=student.id, body=normalized_body, post_type="question"))
    db.commit()
    db.refresh(thread)
    return next(item for item in list_student_qna_threads(db, course=course, student=student) if item["id"] == thread.id)


def answer_qna_thread(db: Session, *, course: Course, professor: User, thread_id: int, body: str, close: bool) -> dict:
    normalized_body = body.strip()
    if not normalized_body:
        raise _api_error(400, "QNA_INVALID_PAYLOAD", "answer body is required")
    thread = db.scalar(select(CourseQnaThread).where(CourseQnaThread.id == thread_id, CourseQnaThread.course_id == course.id))
    if thread is None:
        raise _api_error(404, "QNA_THREAD_NOT_FOUND", "qna thread not found", {"thread_id": thread_id})
    thread.status = "closed" if close else "answered"
    thread.updated_at = _utcnow()
    db.add(CourseQnaPost(thread_id=thread.id, author_user_id=professor.id, body=normalized_body, post_type="answer"))
    db.add(thread)
    db.commit()
    return next(item for item in list_professor_qna_threads(db, course=course) if item["id"] == thread.id)


def _progress_payload(item: LearningItem, student: User, progress: LearningProgress | None) -> dict:
    return {
        "learning_item_id": item.id,
        "learning_item_title": item.title,
        "title": item.title,
        "kind": item.item_type,
        "student_id": student.student_id or "",
        "student_name": student.name,
        "progress_percent": float(progress.progress_percent) if progress else 0,
        "status": progress.status if progress else "not_started",
        "last_viewed_at": progress.last_viewed_at if progress else None,
        "completed_at": progress.completed_at if progress else None,
        "updated_at": progress.updated_at if progress else None,
    }


def list_student_learning_progress(db: Session, *, course: Course, student: User) -> list[dict]:
    items = list(
        db.scalars(
            select(LearningItem)
            .where(LearningItem.course_id == course.id, LearningItem.is_published.is_(True))
            .order_by(LearningItem.sort_order.asc(), LearningItem.created_at.desc(), LearningItem.id.desc())
        )
    )
    progress_rows = list(
        db.scalars(
            select(LearningProgress).where(
                LearningProgress.student_user_id == student.id,
                LearningProgress.learning_item_id.in_([item.id for item in items] or [0]),
            )
        )
    )
    progress_index = {progress.learning_item_id: progress for progress in progress_rows}
    return [_progress_payload(item, student, progress_index.get(item.id)) for item in items]


def update_student_learning_progress(
    db: Session,
    *,
    course: Course,
    student: User,
    learning_item_id: int,
    progress_percent: int,
    status: str,
) -> dict:
    if status not in LEARNING_PROGRESS_STATUSES or progress_percent < 0 or progress_percent > 100:
        raise _api_error(400, "LEARNING_PROGRESS_INVALID_PAYLOAD", "invalid learning progress payload")
    item = db.scalar(select(LearningItem).where(LearningItem.id == learning_item_id, LearningItem.course_id == course.id))
    if item is None:
        raise _api_error(404, "LEARNING_ITEM_NOT_FOUND", "learning item not found", {"learning_item_id": learning_item_id})
    now = _utcnow()
    if status == "completed":
        progress_percent = 100
    progress = db.scalar(
        select(LearningProgress).where(
            LearningProgress.learning_item_id == item.id,
            LearningProgress.student_user_id == student.id,
        )
    )
    if progress is None:
        progress = LearningProgress(learning_item_id=item.id, student_user_id=student.id)
    progress.progress_percent = progress_percent
    progress.status = status
    progress.last_viewed_at = now
    progress.completed_at = now if status == "completed" else None
    progress.updated_at = now
    db.add(progress)
    db.commit()
    db.refresh(progress)
    return _progress_payload(item, student, progress)


def build_professor_learning_progress(db: Session, *, course: Course) -> list[dict]:
    students = list(
        db.scalars(
            select(User)
            .join(CourseEnrollment, CourseEnrollment.student_user_id == User.id)
            .where(CourseEnrollment.course_id == course.id, CourseEnrollment.status == "active")
            .order_by(User.student_id.asc(), User.id.asc())
        )
    )
    rows: list[dict] = []
    for student in students:
        rows.extend(list_student_learning_progress(db, course=course, student=student))
    return rows
