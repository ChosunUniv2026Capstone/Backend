from datetime import datetime

from pydantic import ConfigDict
from pydantic import BaseModel, Field


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
    course_id: str
    classroom_id: str
    purpose: str = Field(default="attendance")


class AttendanceEligibilityResponse(BaseModel):
    eligible: bool
    reason_code: str
    matched_device_mac: str | None = None
    observed_at: datetime | None = None
    snapshot_age_seconds: int | None = None
    evidence: dict
