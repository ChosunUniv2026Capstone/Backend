from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class HealthResponse(BaseModel):
    status: str


class AuthLoginRequest(BaseModel):
    login_id: str
    password: str


class AuthUser(BaseModel):
    id: int
    role: str
    login_id: str
    name: str


class AuthLoginResponse(BaseModel):
    access_token: str
    user: AuthUser


class CourseRead(BaseModel):
    id: int
    course_code: str
    title: str
    professor_name: str | None = None
    classroom_code: str | None = None


class NoticeCreate(BaseModel):
    title: str
    body: str
    course_code: str | None = None


class NoticeRead(BaseModel):
    id: int
    title: str
    body: str
    course_code: str | None = None
    author_name: str
    created_at: datetime | None = None


class NoticeListResponse(BaseModel):
    success: Literal[True] = True
    data: list[NoticeRead]
    message: str = "ok"
    meta: dict[str, Any] = Field(default_factory=dict)


class NoticeResponse(BaseModel):
    success: Literal[True] = True
    data: NoticeRead
    message: str = "ok"
    meta: dict[str, Any] = Field(default_factory=dict)


class ExamSummaryRead(BaseModel):
    id: int
    title: str
    description: str | None = None
    exam_type: str
    status: str
    starts_at: datetime
    ends_at: datetime
    duration_minutes: int
    requires_presence: bool
    late_entry_allowed: bool = True
    auto_submit_enabled: bool = True
    shuffle_questions: bool = False
    shuffle_options: bool = False
    max_attempts: int
    question_count: int = 0
    attempt_count: int = 0


class StudentExamAvailabilityRead(BaseModel):
    code: str
    label: str
    can_start: bool
    can_submit: bool


class StudentExamAttemptRead(BaseModel):
    id: int
    attempt_no: int
    status: str
    started_at: datetime | None = None
    submitted_at: datetime | None = None
    expires_at: datetime | None = None
    score: float | None = None
    total_count: int = 0
    answered_count: int = 0


class StudentExamSummaryRead(ExamSummaryRead):
    attempts_used: int
    availability: StudentExamAvailabilityRead | None = None
    attempt: StudentExamAttemptRead | None = None


class ExamQuestionOptionRead(BaseModel):
    id: int
    option_order: int
    option_text: str
    is_correct: bool | None = None


class StudentExamQuestionRead(BaseModel):
    id: int
    question_order: int
    question_type: str
    prompt: str
    points: float
    explanation: str | None = None
    is_required: bool = True
    selected_option_id: int | None = None
    options: list[ExamQuestionOptionRead] = Field(default_factory=list)


class StudentExamDetailRead(StudentExamSummaryRead):
    questions: list[StudentExamQuestionRead] = Field(default_factory=list)


class ProfessorExamQuestionRead(StudentExamQuestionRead):
    options: list[ExamQuestionOptionRead] = Field(default_factory=list)


class ProfessorExamChoiceCreateRequest(BaseModel):
    option_text: str
    is_correct: bool = False


class ProfessorExamQuestionCreateRequest(BaseModel):
    question_type: Literal["multiple_choice", "true_false"] = "multiple_choice"
    prompt: str
    points: float = Field(default=1, gt=0)
    explanation: str | None = None
    is_required: bool = True
    options: list[ProfessorExamChoiceCreateRequest] = Field(default_factory=list)


class ProfessorExamCreateRequest(BaseModel):
    title: str
    description: str | None = None
    exam_type: Literal["quiz", "midterm", "final", "practice", "custom"] = "quiz"
    starts_at: datetime
    ends_at: datetime
    duration_minutes: int = Field(gt=0)
    requires_presence: bool = False
    late_entry_allowed: bool = True
    auto_submit_enabled: bool = True
    shuffle_questions: bool = False
    shuffle_options: bool = False
    max_attempts: int = Field(default=1, gt=0)
    questions: list[ProfessorExamQuestionCreateRequest] = Field(default_factory=list)


class ProfessorExamSubmissionOverviewRead(BaseModel):
    total_students: int = 0
    started_students: int = 0
    submitted_students: int = 0
    not_started_students: int = 0
    average_score: float | None = None
    max_score: float = 0


class ProfessorExamSubmissionSummaryRead(BaseModel):
    student_id: str
    student_name: str
    status: str
    attempt_no: int | None = None
    answered_count: int = 0
    started_at: datetime | None = None
    submitted_at: datetime | None = None
    score: float | None = None
    max_score: float = 0
    total_count: int = 0


class ProfessorExamDetailRead(ExamSummaryRead):
    questions: list[ProfessorExamQuestionRead] = Field(default_factory=list)
    submission_overview: ProfessorExamSubmissionOverviewRead | None = None
    submissions: list[ProfessorExamSubmissionSummaryRead] = Field(default_factory=list)


class ExamSubmissionStartRead(BaseModel):
    submission_id: int
    attempt_no: int
    status: str
    started_at: datetime
    expires_at: datetime
    idempotent: bool = False


class StudentExamSubmitAnswerRequest(BaseModel):
    question_id: int
    selected_option_id: int | None = None
    answer_text: str | None = None


class StudentExamSubmitRequest(BaseModel):
    answers: list[StudentExamSubmitAnswerRequest] = Field(default_factory=list)


class StudentExamSaveAnswerRequest(BaseModel):
    selected_option_id: int | None = None
    answer_text: str | None = None


class StudentExamSaveAnswerRead(BaseModel):
    submission_id: int
    question_id: int
    selected_option_id: int | None = None
    answer_text: str | None = None
    answered_at: datetime | None = None


class StudentExamSubmitResultRead(BaseModel):
    exam_id: int
    attempt: StudentExamAttemptRead
    score: float | None = None
    total_count: int = 0
    answered_count: int = 0


class UserSummary(BaseModel):
    id: int
    role: str
    login_id: str
    name: str


class ClassroomRead(BaseModel):
    id: int
    classroom_code: str
    name: str
    building: str | None = None
    floor_label: str | None = None


class ClassroomNetworkRead(BaseModel):
    id: int
    classroom_code: str
    ap_id: str
    ssid: str
    gateway_host: str | None = None
    signal_threshold_dbm: int | None = None
    collection_mode: str


class DeviceCreate(BaseModel):
    label: str
    mac_address: str


class DeviceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    student_id: str
    label: str
    mac_address: str
    status: str
    created_at: datetime | None = None


class AttendanceEligibilityRequest(BaseModel):
    student_id: str
    course_code: str


class AttendanceEligibilityResponse(BaseModel):
    eligible: bool
    reason_code: str
    matched_device_mac: str | None = None
    observed_at: datetime | None = None
    snapshot_age_seconds: int | None = None
    evidence: dict


class AdminPresenceStationMutation(BaseModel):
    mac_address: str = Field(alias="macAddress")
    ap_id: str | None = Field(default=None, alias="apId")
    present: bool = True
    associated: bool | None = None
    authorized: bool | None = None
    authenticated: bool | None = None
    signal_dbm: int | None = Field(default=None, alias="signalDbm")
    connected_seconds: int | None = Field(default=None, alias="connectedSeconds")
    rx_bytes: int | None = Field(default=None, alias="rxBytes")
    tx_bytes: int | None = Field(default=None, alias="txBytes")


class AdminPresenceSnapshotMutationRequest(BaseModel):
    stations: list[AdminPresenceStationMutation] = Field(default_factory=list)


class AdminPresenceStationRead(BaseModel):
    mac_address: str = Field(alias="macAddress")
    associated: bool | None = None
    authenticated: bool | None = None
    authorized: bool | None = None
    signal_dbm: int | None = Field(default=None, alias="signalDbm")
    connected_seconds: int | None = Field(default=None, alias="connectedSeconds")
    rx_bytes: int | None = Field(default=None, alias="rxBytes")
    tx_bytes: int | None = Field(default=None, alias="txBytes")
    device_label: str | None = Field(default=None, alias="deviceLabel")
    owner_name: str | None = Field(default=None, alias="ownerName")
    owner_login_id: str | None = Field(default=None, alias="ownerLoginId")


class AdminPresenceDeviceOptionRead(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    student_login_id: str | None = Field(default=None, alias="studentLoginId")
    student_name: str | None = Field(default=None, alias="studentName")
    device_label: str | None = Field(default=None, alias="deviceLabel")
    mac_address: str = Field(alias="macAddress")
    observed: bool = False


class AdminPresenceAccessPointRead(BaseModel):
    ap_id: str = Field(alias="apId")
    ssid: str
    source_command: str = Field(alias="sourceCommand")
    stations: list[AdminPresenceStationRead]


class AdminPresenceSnapshotRead(BaseModel):
    cache_hit: bool = Field(alias="cacheHit")
    overlay_active: bool = Field(alias="overlayActive")
    classroom_code: str = Field(alias="classroomCode")
    observed_at: datetime | None = Field(default=None, alias="observedAt")
    collection_mode: str | None = Field(default=None, alias="collectionMode")
    aps: list[AdminPresenceAccessPointRead]
    classroom_networks: list[ClassroomNetworkRead] = Field(default_factory=list, alias="classroomNetworks")
    device_options: list[AdminPresenceDeviceOptionRead] = Field(default_factory=list, alias="deviceOptions")


class AdminClassroomNetworkThresholdUpdate(BaseModel):
    signal_threshold_dbm: int | None = None


class AttendanceSessionBatchRequest(BaseModel):
    projection_keys: list[str] = Field(default_factory=list)
    mode: Literal["manual", "smart", "canceled"]


class AttendanceRecordUpdateRequest(BaseModel):
    status: Literal["present", "absent", "late", "official", "sick"]
    reason: str | None = ""
    projection_key: str | None = None
