from datetime import datetime, time

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, JSON, Numeric, String, Text, Time, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    student_id: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=True)
    professor_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=True)
    admin_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=True)
    name: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(16))
    password: Mapped[str] = mapped_column(String(120))


class RegisteredDevice(Base):
    __tablename__ = "registered_devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    label: Mapped[str] = mapped_column(String(100))
    mac_address: Mapped[str] = mapped_column(String(17), unique=True)
    status: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CourseEnrollment(Base):
    __tablename__ = "course_enrollments"
    __table_args__ = (UniqueConstraint("course_id", "student_user_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"))
    student_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(16), default="enrolled")


class Course(Base):
    __tablename__ = "courses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_code: Mapped[str] = mapped_column(String(32), unique=True)
    title: Mapped[str] = mapped_column(String(120))
    professor_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=True)


class Classroom(Base):
    __tablename__ = "classrooms"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    classroom_code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    building: Mapped[str] = mapped_column(String(120), nullable=True)
    floor_label: Mapped[str] = mapped_column(String(32), nullable=True)


class CourseSchedule(Base):
    __tablename__ = "course_schedules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"))
    classroom_id: Mapped[int] = mapped_column(ForeignKey("classrooms.id"))
    day_of_week: Mapped[int] = mapped_column(Integer)
    starts_at: Mapped[time] = mapped_column(Time)
    ends_at: Mapped[time] = mapped_column(Time)


class ClassroomNetwork(Base):
    __tablename__ = "classroom_networks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    classroom_id: Mapped[int] = mapped_column(ForeignKey("classrooms.id"))
    ap_id: Mapped[str] = mapped_column(String(64))
    ssid: Mapped[str] = mapped_column(String(120))
    gateway_host: Mapped[str] = mapped_column(String(120), nullable=True)
    signal_threshold_dbm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    collection_mode: Mapped[str] = mapped_column(String(40))


class AccessPoint(Base):
    __tablename__ = "access_points"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    collector_ap_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    label: Mapped[str] = mapped_column(String(120))
    management_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tailnet_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active")
    token_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    token_version: Mapped[int] = mapped_column(Integer, default=0)
    token_revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AccessPointInterface(Base):
    __tablename__ = "access_point_interfaces"
    __table_args__ = (
        UniqueConstraint("access_point_id", "interface_id"),
        UniqueConstraint("classroom_network_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    access_point_id: Mapped[int] = mapped_column(ForeignKey("access_points.id"), index=True)
    interface_id: Mapped[str] = mapped_column(String(64))
    bssid: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ssid: Mapped[str | None] = mapped_column(String(120), nullable=True)
    classroom_network_id: Mapped[int] = mapped_column(ForeignKey("classroom_networks.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PresenceEligibilityLog(Base):
    __tablename__ = "presence_eligibility_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    student_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    course_id: Mapped[int | None] = mapped_column(ForeignKey("courses.id"), nullable=True)
    classroom_id: Mapped[int | None] = mapped_column(ForeignKey("classrooms.id"), nullable=True)
    purpose: Mapped[str] = mapped_column(String(20))
    eligible: Mapped[bool] = mapped_column(Boolean)
    reason_code: Mapped[str] = mapped_column(String(64))
    matched_device_mac: Mapped[str | None] = mapped_column(String(17), nullable=True)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    snapshot_age_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Notice(Base):
    __tablename__ = "notices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), nullable=True)
    author_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Assignment(Base):
    __tablename__ = "assignments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    opens_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class AssignmentSubmission(Base):
    __tablename__ = "assignment_submissions"
    __table_args__ = (UniqueConstraint("assignment_id", "student_user_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    assignment_id: Mapped[int] = mapped_column(ForeignKey("assignments.id"), index=True)
    student_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    submission_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class AssignmentSubmissionAttachment(Base):
    __tablename__ = "assignment_submission_attachments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("assignment_submissions.id"), index=True)
    original_filename: Mapped[str] = mapped_column(String(255))
    stored_filename: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    file_size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    storage_provider: Mapped[str] = mapped_column(String(20), default="local")
    bucket_name: Mapped[str] = mapped_column(String(120), default="local")
    storage_key: Mapped[str] = mapped_column(String(500))
    checksum_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Exam(Base):
    __tablename__ = "exams"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    exam_type: Mapped[str] = mapped_column(String(20), default="quiz")
    status: Mapped[str] = mapped_column(String(20), default="draft")
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    duration_minutes: Mapped[int] = mapped_column(Integer)
    requires_presence: Mapped[bool] = mapped_column(Boolean, default=True)
    late_entry_allowed: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_submit_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    shuffle_questions: Mapped[bool] = mapped_column(Boolean, default=False)
    shuffle_options: Mapped[bool] = mapped_column(Boolean, default=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class ExamQuestion(Base):
    __tablename__ = "exam_questions"
    __table_args__ = (
        UniqueConstraint("exam_id", "question_order"),
        UniqueConstraint("id", "exam_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("exams.id"), index=True)
    question_order: Mapped[int] = mapped_column(Integer)
    question_type: Mapped[str] = mapped_column(String(30))
    prompt: Mapped[str] = mapped_column(Text)
    points: Mapped[float] = mapped_column(Numeric(6, 2), default=1)
    correct_answer_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_required: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class ExamQuestionOption(Base):
    __tablename__ = "exam_question_options"
    __table_args__ = (
        UniqueConstraint("question_id", "option_order"),
        UniqueConstraint("id", "question_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("exam_questions.id"), index=True)
    option_order: Mapped[int] = mapped_column(Integer)
    option_text: Mapped[str] = mapped_column(Text)
    is_correct: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ExamSubmission(Base):
    __tablename__ = "exam_submissions"
    __table_args__ = (
        UniqueConstraint("exam_id", "student_user_id", "attempt_no"),
        UniqueConstraint("id", "exam_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("exams.id"), index=True)
    student_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    attempt_no: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default="in_progress")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    time_limit_snapshot_minutes: Mapped[int] = mapped_column(Integer)
    score: Mapped[float | None] = mapped_column(Numeric(8, 2), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class ExamSubmissionAnswer(Base):
    __tablename__ = "exam_submission_answers"
    __table_args__ = (UniqueConstraint("submission_id", "question_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("exams.id"), index=True)
    submission_id: Mapped[int] = mapped_column(Integer, index=True)
    question_id: Mapped[int] = mapped_column(Integer, index=True)
    selected_option_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    answer_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    awarded_score: Mapped[float | None] = mapped_column(Numeric(8, 2), nullable=True)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class LearningItem(Base):
    __tablename__ = "learning_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), index=True)
    created_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    item_type: Mapped[str] = mapped_column(String(20), default="file")
    external_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_published: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class LearningItemAttachment(Base):
    __tablename__ = "learning_item_attachments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    learning_item_id: Mapped[int] = mapped_column(ForeignKey("learning_items.id"), index=True)
    original_filename: Mapped[str] = mapped_column(String(255))
    stored_filename: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    file_size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    storage_provider: Mapped[str] = mapped_column(String(20), default="local")
    bucket_name: Mapped[str] = mapped_column(String(120), default="local")
    storage_key: Mapped[str] = mapped_column(String(700))
    checksum_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class NoticeAttachment(Base):
    __tablename__ = "notice_attachments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    notice_id: Mapped[int] = mapped_column(ForeignKey("notices.id"), index=True)
    original_filename: Mapped[str] = mapped_column(String(255))
    stored_filename: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    file_size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    storage_provider: Mapped[str] = mapped_column(String(20), default="local")
    bucket_name: Mapped[str] = mapped_column(String(120), default="local")
    storage_key: Mapped[str] = mapped_column(String(700))
    checksum_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ExamQuestionAttachment(Base):
    __tablename__ = "exam_question_attachments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("exam_questions.id"), index=True)
    attachment_role: Mapped[str] = mapped_column(String(20), default="prompt")
    original_filename: Mapped[str] = mapped_column(String(255))
    stored_filename: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    file_size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    storage_provider: Mapped[str] = mapped_column(String(20), default="local")
    bucket_name: Mapped[str] = mapped_column(String(120), default="local")
    storage_key: Mapped[str] = mapped_column(String(700))
    checksum_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReportExport(Base):
    __tablename__ = "report_exports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int | None] = mapped_column(ForeignKey("courses.id"), index=True, nullable=True)
    requested_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    report_domain: Mapped[str] = mapped_column(String(20), default="attendance")
    export_format: Mapped[str] = mapped_column(String(20), default="csv")
    original_filename: Mapped[str] = mapped_column(String(255))
    stored_filename: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    file_size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    storage_provider: Mapped[str] = mapped_column(String(20), default="local")
    bucket_name: Mapped[str] = mapped_column(String(120), default="local")
    storage_key: Mapped[str] = mapped_column(String(700))
    checksum_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="ready")
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AttendanceSession(Base):
    __tablename__ = "attendance_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    projection_key: Mapped[str] = mapped_column(String(255), index=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), index=True)
    classroom_id: Mapped[int] = mapped_column(ForeignKey("classrooms.id"), index=True)
    session_date: Mapped[datetime.date] = mapped_column(Date)
    slot_start_at: Mapped[time] = mapped_column(Time)
    slot_end_at: Mapped[time] = mapped_column(Time)
    mode: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="active")
    opened_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    latest_version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class AttendanceSessionSlot(Base):
    __tablename__ = "attendance_session_slots"
    __table_args__ = (UniqueConstraint("attendance_session_id", "projection_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    attendance_session_id: Mapped[int] = mapped_column(ForeignKey("attendance_sessions.id"), index=True)
    projection_key: Mapped[str] = mapped_column(String(255), index=True)
    classroom_id: Mapped[int] = mapped_column(ForeignKey("classrooms.id"), index=True)
    session_date: Mapped[datetime.date] = mapped_column(Date)
    slot_start_at: Mapped[time] = mapped_column(Time)
    slot_end_at: Mapped[time] = mapped_column(Time)
    slot_order: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AttendanceRecord(Base):
    __tablename__ = "attendance_records"
    __table_args__ = (UniqueConstraint("attendance_session_id", "projection_key", "student_user_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    attendance_session_id: Mapped[int] = mapped_column(ForeignKey("attendance_sessions.id"), index=True)
    projection_key: Mapped[str] = mapped_column(String(255), index=True)
    student_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    final_status: Mapped[str] = mapped_column(String(16))
    attendance_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    finalized_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class AttendanceStatusAuditLog(Base):
    __tablename__ = "attendance_status_audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    attendance_session_id: Mapped[int] = mapped_column(ForeignKey("attendance_sessions.id"), index=True)
    projection_key: Mapped[str] = mapped_column(String(255), index=True)
    student_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    actor_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    actor_role: Mapped[str] = mapped_column(String(16))
    change_source: Mapped[str] = mapped_column(String(32))
    previous_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    new_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    version: Mapped[int] = mapped_column(Integer, default=0)


class RefreshSession(Base):
    __tablename__ = "refresh_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    current_token_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    replay_detected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
