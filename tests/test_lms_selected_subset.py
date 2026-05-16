from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from envelope import api_json

from app.db import get_db
from app.main import app
from app.models import (
    Assignment,
    AssignmentSubmission,
    Base,
    Course,
    CourseEnrollment,
    Exam,
    ExamQuestion,
    ExamSubmission,
    LearningItem,
    LearningProgress,
    User,
)


def auth_header(login_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer dev-token:{login_id}"}


def make_client() -> tuple[TestClient, sessionmaker]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    Base.metadata.create_all(engine)

    with session_factory.begin() as session:
        seed_selected_lms_state(session)

    def override_get_db() -> Generator[Session, None, None]:
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app), session_factory


def seed_selected_lms_state(session: Session) -> None:
    student = User(student_id="20201239", name="Kim Student 06", role="student", password="devpass123")
    other_student = User(student_id="20201234", name="Other Student", role="student", password="devpass123")
    professor = User(professor_id="PRF002", name="Lee Professor 02", role="professor", password="devpass123")
    other_professor = User(professor_id="PRF999", name="Other Professor", role="professor", password="devpass123")
    session.add_all([student, other_student, professor, other_professor])
    session.flush()

    course = Course(course_code="CSE116", title="Capstone Design A", professor_user_id=professor.id)
    other_course = Course(course_code="CSE999", title="Other Course", professor_user_id=other_professor.id)
    session.add_all([course, other_course])
    session.flush()
    session.add(CourseEnrollment(course_id=course.id, student_user_id=student.id, status="active"))
    session.add(CourseEnrollment(course_id=other_course.id, student_user_id=other_student.id, status="active"))

    now = datetime.now(UTC)
    assignment = Assignment(
        course_id=course.id,
        title="Design Report",
        description="Submit report",
        opens_at=now - timedelta(days=1),
        due_at=now + timedelta(days=1),
        max_score=100,
    )
    exam = Exam(
        course_id=course.id,
        title="Midterm",
        exam_type="quiz",
        status="closed",
        starts_at=now - timedelta(days=2),
        ends_at=now - timedelta(days=1),
        duration_minutes=30,
        requires_presence=False,
    )
    item = LearningItem(
        course_id=course.id,
        created_by_user_id=professor.id,
        title="Week 1 Video",
        item_type="video",
        is_published=True,
    )
    session.add_all([assignment, exam, item])
    session.flush()
    session.add(
        AssignmentSubmission(
            assignment_id=assignment.id,
            student_user_id=student.id,
            submission_text="done",
        )
    )
    session.add(ExamQuestion(exam_id=exam.id, question_order=1, question_type="short_answer", prompt="Explain", points=50))
    session.add(
        ExamSubmission(
            exam_id=exam.id,
            student_user_id=student.id,
            attempt_no=1,
            status="graded",
            started_at=now - timedelta(days=2),
            submitted_at=now - timedelta(days=1, hours=23),
            expires_at=now - timedelta(days=1, hours=23, minutes=30),
            time_limit_snapshot_minutes=30,
            score=40,
        )
    )


def test_professor_grades_submission_and_student_sees_grade_summary() -> None:
    client, _ = make_client()

    before = client.get("/api/students/20201239/courses/CSE116/grades", headers=auth_header("20201239"))
    assert before.status_code == 200
    before_payload = api_json(before)
    assert before_payload["assignments"][0]["score"] is None
    assert before_payload["items"][0]["item_type"] == "assignment"
    assert before_payload["items"][0]["item_id"] == 1
    assert before_payload["exams"][0]["score"] == 40
    assert before_payload["items"][1]["item_type"] == "exam"

    graded = client.put(
        "/api/professors/PRF002/courses/CSE116/assignments/1/submissions/1/grade",
        headers=auth_header("PRF002"),
        json={"score": 95, "feedback": "Strong submission", "grading_status": "graded"},
    )
    assert graded.status_code == 200
    graded_payload = api_json(graded)
    assert graded_payload["submissions"][0]["score"] == 95
    assert graded_payload["submissions"][0]["feedback"] == "Strong submission"

    after = client.get("/api/students/20201239/courses/CSE116/grades", headers=auth_header("20201239"))
    assert after.status_code == 200
    after_payload = api_json(after)
    assert after_payload["assignments"][0]["score"] == 95
    assert after_payload["assignments"][0]["feedback"] == "Strong submission"
    assert after_payload["items"][0]["feedback"] == "Strong submission"
    assert after_payload["overall_percent"] == 87.5

    professor_grades = client.get("/api/professors/PRF002/courses/CSE116/grades", headers=auth_header("PRF002"))
    assert professor_grades.status_code == 200
    professor_payload = api_json(professor_grades)
    assert len(professor_payload) == 1
    assert professor_payload[0]["items"][0]["item_type"] == "assignment"


def test_selected_lms_rbac_rejects_cross_course_grade_access() -> None:
    client, _ = make_client()

    other_student = client.get("/api/students/20201234/courses/CSE116/grades", headers=auth_header("20201234"))
    assert other_student.status_code == 403
    assert other_student.json()["error"]["code"] == "FORBIDDEN"

    other_professor = client.get("/api/professors/PRF999/courses/CSE116/grades", headers=auth_header("PRF999"))
    assert other_professor.status_code == 403
    assert other_professor.json()["error"]["code"] == "FORBIDDEN"


def test_qna_student_create_and_professor_answer_are_enveloped() -> None:
    client, _ = make_client()

    created = client.post(
        "/api/students/20201239/courses/CSE116/qna",
        headers=auth_header("20201239"),
        json={"title": "Can I revise?", "body": "Can I resubmit the report?"},
    )
    assert created.status_code == 201
    created_payload = api_json(created)
    assert created_payload["status"] == "open"
    assert created_payload["posts"][0]["post_type"] == "question"

    answered = client.post(
        f"/api/professors/PRF002/courses/CSE116/qna/{created_payload['id']}/answer",
        headers=auth_header("PRF002"),
        json={"body": "Yes, before the deadline.", "close": True},
    )
    assert answered.status_code == 201
    answered_payload = api_json(answered)
    assert answered_payload["status"] == "closed"
    assert [post["post_type"] for post in answered_payload["posts"]] == ["question", "answer"]

    listed = client.get("/api/students/20201239/courses/CSE116/qna", headers=auth_header("20201239"))
    assert listed.status_code == 200
    assert api_json(listed)[0]["posts"][-1]["body"] == "Yes, before the deadline."


def test_student_learning_progress_upsert_and_professor_snapshot() -> None:
    client, session_factory = make_client()

    empty = client.get("/api/students/20201239/courses/CSE116/learning-progress", headers=auth_header("20201239"))
    assert empty.status_code == 200
    assert api_json(empty)[0]["status"] == "not_started"

    updated = client.put(
        "/api/students/20201239/courses/CSE116/learning-items/1/progress",
        headers=auth_header("20201239"),
        json={"progress_percent": 80, "status": "completed"},
    )
    assert updated.status_code == 200
    updated_payload = api_json(updated)
    assert updated_payload["progress_percent"] == 100
    assert updated_payload["status"] == "completed"

    with session_factory() as session:
        saved = session.scalar(select(LearningProgress))
        assert saved is not None
        assert saved.progress_percent == 100

    snapshot = client.get("/api/professors/PRF002/courses/CSE116/learning-progress", headers=auth_header("PRF002"))
    assert snapshot.status_code == 200
    snapshot_payload = api_json(snapshot)
    assert snapshot_payload[0]["student_id"] == "20201239"
    assert snapshot_payload[0]["title"] == "Week 1 Video"
    assert snapshot_payload[0]["status"] == "completed"


def test_grade_endpoint_rejects_invalid_payload_and_course_scope() -> None:
    client, _ = make_client()

    bad_status = client.put(
        "/api/professors/PRF002/courses/CSE116/assignments/1/submissions/1/grade",
        headers=auth_header("PRF002"),
        json={"score": 95, "feedback": "x", "grading_status": "invalid"},
    )
    assert bad_status.status_code == 422

    over_score = client.put(
        "/api/professors/PRF002/courses/CSE116/assignments/1/submissions/1/grade",
        headers=auth_header("PRF002"),
        json={"score": 150, "feedback": "x", "grading_status": "graded"},
    )
    assert over_score.status_code == 400
    assert over_score.json()["error"]["code"] == "ASSIGNMENT_INVALID_GRADE"

    no_access = client.put(
        "/api/professors/PRF999/courses/CSE116/assignments/1/submissions/1/grade",
        headers=auth_header("PRF999"),
        json={"score": 80, "feedback": "no", "grading_status": "graded"},
    )
    assert no_access.status_code == 403

    missing = client.put(
        "/api/professors/PRF002/courses/CSE116/assignments/999/submissions/1/grade",
        headers=auth_header("PRF002"),
        json={"score": 80, "feedback": "no", "grading_status": "graded"},
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "ASSIGNMENT_NOT_FOUND"


def test_grade_endpoint_rejects_invalid_submission_and_empty_feedback() -> None:
    client, _ = make_client()

    missing_submission = client.put(
        "/api/professors/PRF002/courses/CSE116/assignments/1/submissions/999/grade",
        headers=auth_header("PRF002"),
        json={"score": 80, "feedback": "No record", "grading_status": "graded"},
    )
    assert missing_submission.status_code == 404
    assert missing_submission.json()["error"]["code"] == "ASSIGNMENT_SUBMISSION_NOT_FOUND"


def test_qna_validation_and_thread_update_status() -> None:
    client, _ = make_client()

    create_blank = client.post(
        "/api/students/20201239/courses/CSE116/qna",
        headers=auth_header("20201239"),
        json={"title": "   ", "body": "   "},
    )
    assert create_blank.status_code == 400
    assert create_blank.json()["error"]["code"] == "QNA_INVALID_PAYLOAD"

    created = client.post(
        "/api/students/20201239/courses/CSE116/qna",
        headers=auth_header("20201239"),
        json={"title": "Can I revise?", "body": "Can I resubmit the report?"},
    )
    payload = api_json(created)

    answer_empty = client.post(
        f"/api/professors/PRF002/courses/CSE116/qna/{payload['id']}/answer",
        headers=auth_header("PRF002"),
        json={"body": "", "close": False},
    )
    assert answer_empty.status_code == 400
    assert answer_empty.json()["error"]["code"] == "QNA_INVALID_PAYLOAD"

    answered = client.post(
        f"/api/professors/PRF002/courses/CSE116/qna/{payload['id']}/answer",
        headers=auth_header("PRF002"),
        json={"body": "Yes, before the deadline.", "close": False},
    )
    assert answered.status_code == 201
    assert api_json(answered)["status"] == "answered"


def test_learning_progress_requires_valid_inputs_and_active_items_only() -> None:
    client, session_factory = make_client()

    too_high = client.put(
        "/api/students/20201239/courses/CSE116/learning-items/1/progress",
        headers=auth_header("20201239"),
        json={"progress_percent": 120, "status": "completed"},
    )
    assert too_high.status_code == 422

    bad_status = client.put(
        "/api/students/20201239/courses/CSE116/learning-items/1/progress",
        headers=auth_header("20201239"),
        json={"progress_percent": 50, "status": "invalid"},
    )
    assert bad_status.status_code == 422

    with session_factory() as session:
        course = session.scalar(select(Course).where(Course.course_code == "CSE116"))
        student = session.scalar(select(User).where(User.student_id == "20201239"))
        assert course is not None and student is not None
        session.add(
            LearningItem(
                course_id=course.id,
                created_by_user_id=student.id,
                title="Draft Item",
                item_type="video",
                is_published=False,
            )
        )
        session.commit()

        items = api_json(client.get("/api/students/20201239/courses/CSE116/learning-progress", headers=auth_header("20201239")))
        assert len(items) == 1

    no_item = client.put(
        "/api/students/20201239/courses/CSE116/learning-items/999/progress",
        headers=auth_header("20201239"),
        json={"progress_percent": 10, "status": "in_progress"},
    )
    assert no_item.status_code == 404
    assert no_item.json()["error"]["code"] == "LEARNING_ITEM_NOT_FOUND"
