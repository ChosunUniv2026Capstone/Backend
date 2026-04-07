from datetime import datetime, time

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, Time, UniqueConstraint, func
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


class Notice(Base):
    __tablename__ = "notices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), nullable=True)
    author_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(String)
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


class AttendanceRecord(Base):
    __tablename__ = "attendance_records"
    __table_args__ = (UniqueConstraint("attendance_session_id", "student_user_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    attendance_session_id: Mapped[int] = mapped_column(ForeignKey("attendance_sessions.id"), index=True)
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
