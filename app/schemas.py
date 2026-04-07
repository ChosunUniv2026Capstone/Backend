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
    course_id: str
    classroom_id: str | None = None
    purpose: str = Field(default="attendance")


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
