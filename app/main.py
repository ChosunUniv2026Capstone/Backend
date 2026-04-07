from fastapi import Depends, FastAPI, Header, HTTPException, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import RegisteredDevice, User
from app.presence_client import PresenceClient
from app.schemas import (
    AdminClassroomNetworkThresholdUpdate,
    AdminPresenceSnapshotMutationRequest,
    AdminPresenceSnapshotRead,
    AttendanceEligibilityRequest,
    AttendanceEligibilityResponse,
    AuthLoginRequest,
    AuthLoginResponse,
    AuthUser,
    ClassroomNetworkRead,
    ClassroomRead,
    CourseRead,
    DeviceCreate,
    DeviceRead,
    HealthResponse,
    NoticeCreate,
    NoticeRead,
    NoticeListResponse,
    NoticeResponse,
    UserSummary,
)
from app.services import (
    authenticate_user,
    check_attendance_eligibility,
    create_device,
    create_notice,
    delete_device,
    get_notice_detail,
    get_user_by_login_id,
    get_user_login_id,
    list_classroom_networks,
    list_classroom_networks_for_classroom,
    list_classrooms,
    list_devices,
    list_notices,
    list_presence_device_options,
    list_professor_courses,
    list_student_courses,
    list_users,
    update_classroom_network_threshold,
)

settings = get_settings()
presence_client = PresenceClient(settings.presence_service_url)

app = FastAPI(title="Backend", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.cors_origins == "*" else [origin.strip() for origin in settings.cors_origins.split(",")],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def api_error(status_code: int, code: str, message: str, details: dict | None = None) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={
            "code": code,
            "message": message,
            "details": details or {},
        },
    )


def notice_error_response(exc: HTTPException) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, dict) else {}
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "success": False,
            "error": {
                "code": detail.get("code", "NOTICE_REQUEST_FAILED"),
                "message": detail.get("message", "notice request failed"),
                "details": detail.get("details", {}),
            },
        },
    )


def parse_bearer_login_id(authorization: str | None) -> str:
    if not authorization:
        raise api_error(
            status.HTTP_401_UNAUTHORIZED,
            "UNAUTHENTICATED",
            "authentication is required",
        )

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.startswith("dev-token:"):
        raise api_error(
            status.HTTP_401_UNAUTHORIZED,
            "UNAUTHENTICATED",
            "invalid access token",
        )

    login_id = token.removeprefix("dev-token:").strip()
    if not login_id:
        raise api_error(
            status.HTTP_401_UNAUTHORIZED,
            "UNAUTHENTICATED",
            "invalid access token",
        )
    return login_id


def require_authenticated_user(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    login_id = parse_bearer_login_id(authorization)
    try:
        return get_user_by_login_id(db, login_id)
    except HTTPException as exc:
        raise api_error(status.HTTP_401_UNAUTHORIZED, "UNAUTHENTICATED", "invalid access token") from exc


def require_admin_role(current_user: User = Depends(require_authenticated_user)) -> User:
    if current_user.role != "admin":
        raise api_error(status.HTTP_403_FORBIDDEN, "FORBIDDEN", "admin role is required")
    return current_user


def require_student_self(student_id: str, current_user: User) -> None:
    if current_user.role != "student" or get_user_login_id(current_user) != student_id:
        raise api_error(
            status.HTTP_403_FORBIDDEN,
            "FORBIDDEN",
            "requested student does not match the authenticated user",
            {"student_id": student_id},
        )


def require_professor_self(professor_id: str, current_user: User) -> None:
    if current_user.role != "professor" or get_user_login_id(current_user) != professor_id:
        raise api_error(
            status.HTTP_403_FORBIDDEN,
            "FORBIDDEN",
            "requested professor does not match the authenticated user",
            {"professor_id": professor_id},
        )


def require_login_match(requested_login_id: str, current_user: User) -> None:
    if get_user_login_id(current_user) != requested_login_id:
        raise api_error(
            status.HTTP_403_FORBIDDEN,
            "FORBIDDEN",
            "requested user does not match the authenticated user",
            {"requested_login_id": requested_login_id},
        )


def map_presence_snapshot(snapshot_payload: dict, db: Session) -> dict:
    owner_rows = db.execute(
        select(
            RegisteredDevice.mac_address,
            RegisteredDevice.label,
            User.name,
            User.student_id,
            User.professor_id,
            User.admin_id,
        ).join(User, User.id == RegisteredDevice.user_id)
    )
    device_index = {}
    for mac_address, device_label, name, student_id, professor_id, admin_id in owner_rows:
        login_id = student_id or professor_id or admin_id or ""
        device_index[mac_address.lower()] = {
            "deviceLabel": device_label,
            "ownerName": name,
            "ownerLoginId": login_id,
        }

    snapshot = snapshot_payload["snapshot"]
    classroom_code = snapshot.get("classroomId")
    aps = []
    observed_macs: set[str] = set()
    for ap in snapshot.get("aps", []):
        stations = []
        for station in ap.get("stations", []):
            observed_macs.add(station["macAddress"].lower())
            owner = device_index.get(station["macAddress"].lower())
            stations.append(
                {
                    **station,
                    "deviceLabel": owner["deviceLabel"] if owner else None,
                    "ownerName": owner["ownerName"] if owner else None,
                    "ownerLoginId": owner["ownerLoginId"] if owner else None,
                }
            )
        aps.append({**ap, "stations": stations})

    device_options = list_presence_device_options(db, classroom_code)
    device_option_index = {option["mac_address"].lower(): option for option in device_options}
    for ap in aps:
        for station in ap["stations"]:
            mac_address = station["macAddress"].lower()
            if mac_address not in device_option_index:
                device_options.append(
                    {
                        "student_login_id": station.get("ownerLoginId"),
                        "student_name": station.get("ownerName"),
                        "device_label": station.get("deviceLabel"),
                        "mac_address": mac_address,
                        "observed": True,
                    }
                )
            else:
                device_option_index[mac_address]["observed"] = True
    for option in device_options:
        option["observed"] = option["mac_address"].lower() in observed_macs
    device_options.sort(
        key=lambda item: (
            item["student_login_id"] or "zzzz",
            item["student_name"] or "zzzz",
            item["device_label"] or "zzzz",
            item["mac_address"],
        )
    )

    return {
        "cacheHit": snapshot_payload.get("cacheHit", False),
        "overlayActive": snapshot_payload.get("overlayActive", False),
        "classroomCode": classroom_code,
        "observedAt": snapshot.get("observedAt"),
        "collectionMode": snapshot.get("collectionMode"),
        "aps": aps,
        "classroomNetworks": list_classroom_networks_for_classroom(db, classroom_code),
        "deviceOptions": device_options,
    }


@app.get("/health", response_model=HealthResponse)
def health(db: Session = Depends(get_db)) -> HealthResponse:
    db.execute(text("SELECT 1"))
    return HealthResponse(status="ok")


@app.post("/api/auth/login", response_model=AuthLoginResponse)
def login(payload: AuthLoginRequest, db: Session = Depends(get_db)) -> AuthLoginResponse:
    user = authenticate_user(db, payload.login_id, payload.password)
    return AuthLoginResponse(
        access_token=f"dev-token:{get_user_login_id(user)}",
        user=AuthUser(
            id=user.id,
            role=user.role,
            login_id=get_user_login_id(user),
            name=user.name,
        ),
    )


@app.get("/api/students/{student_id}/courses", response_model=list[CourseRead])
def get_student_courses(
    student_id: str,
    current_user: User = Depends(require_authenticated_user),
    db: Session = Depends(get_db),
) -> list[CourseRead]:
    require_student_self(student_id, current_user)
    return [CourseRead(**course) for course in list_student_courses(db, student_id)]


@app.get("/api/professors/{professor_id}/courses", response_model=list[CourseRead])
def get_professor_courses(
    professor_id: str,
    current_user: User = Depends(require_authenticated_user),
    db: Session = Depends(get_db),
) -> list[CourseRead]:
    require_professor_self(professor_id, current_user)
    return [CourseRead(**course) for course in list_professor_courses(db, professor_id)]


@app.get("/api/notices/{login_id}", response_model=NoticeListResponse)
def get_notices(
    login_id: str,
    current_user: User = Depends(require_authenticated_user),
    db: Session = Depends(get_db),
) -> NoticeListResponse | JSONResponse:
    try:
        require_login_match(login_id, current_user)
        notices = [NoticeRead(**notice) for notice in list_notices(db, login_id)]
        return NoticeListResponse(data=notices, meta={"login_id": login_id, "count": len(notices)})
    except HTTPException as exc:
        return notice_error_response(exc)


@app.get("/api/notices/{login_id}/{notice_id}", response_model=NoticeResponse)
def get_notice(
    login_id: str,
    notice_id: int,
    current_user: User = Depends(require_authenticated_user),
    db: Session = Depends(get_db),
) -> NoticeResponse | JSONResponse:
    try:
        require_login_match(login_id, current_user)
        notice = NoticeRead(**get_notice_detail(db, login_id, notice_id))
        return NoticeResponse(data=notice, meta={"login_id": login_id})
    except HTTPException as exc:
        return notice_error_response(exc)


@app.post("/api/professors/{professor_id}/notices", response_model=NoticeResponse, status_code=status.HTTP_201_CREATED)
def add_notice(
    professor_id: str,
    payload: NoticeCreate,
    current_user: User = Depends(require_authenticated_user),
    db: Session = Depends(get_db),
) -> NoticeResponse | JSONResponse:
    try:
        require_professor_self(professor_id, current_user)
        notice = create_notice(db, professor_id, payload.title, payload.body, payload.course_code)
        notices = list_notices(db, professor_id)
        created = next(item for item in notices if item["id"] == notice.id)
        return NoticeResponse(data=NoticeRead(**created), message="created", meta={"professor_id": professor_id})
    except HTTPException as exc:
        return notice_error_response(exc)


@app.get("/api/admin/users", response_model=list[UserSummary])
def get_users(
    _: User = Depends(require_admin_role),
    db: Session = Depends(get_db),
) -> list[UserSummary]:
    return [UserSummary(**user) for user in list_users(db)]


@app.get("/api/admin/classrooms", response_model=list[ClassroomRead])
def get_classrooms(
    _: User = Depends(require_admin_role),
    db: Session = Depends(get_db),
) -> list[ClassroomRead]:
    return [ClassroomRead(**classroom) for classroom in list_classrooms(db)]


@app.get("/api/admin/classroom-networks", response_model=list[ClassroomNetworkRead])
def get_classroom_networks(
    _: User = Depends(require_admin_role),
    db: Session = Depends(get_db),
) -> list[ClassroomNetworkRead]:
    return [ClassroomNetworkRead(**network) for network in list_classroom_networks(db)]


@app.patch("/api/admin/classroom-networks/{network_id}", response_model=ClassroomNetworkRead)
def patch_classroom_network_threshold(
    network_id: int,
    payload: AdminClassroomNetworkThresholdUpdate,
    _: User = Depends(require_admin_role),
    db: Session = Depends(get_db),
) -> ClassroomNetworkRead:
    return ClassroomNetworkRead(**update_classroom_network_threshold(db, network_id, payload.signal_threshold_dbm))


@app.get("/api/admin/presence/classrooms/{classroomCode}/snapshot", response_model=AdminPresenceSnapshotRead)
def get_admin_presence_snapshot(
    classroomCode: str,
    _: User = Depends(require_admin_role),
    db: Session = Depends(get_db),
) -> AdminPresenceSnapshotRead:
    snapshot_payload = presence_client.get_admin_snapshot(classroom_code=classroomCode)
    return AdminPresenceSnapshotRead(**map_presence_snapshot(snapshot_payload, db))


@app.post("/api/admin/presence/classrooms/{classroomCode}/dummy-controls", response_model=AdminPresenceSnapshotRead)
def apply_admin_presence_overlay(
    classroomCode: str,
    payload: AdminPresenceSnapshotMutationRequest,
    _: User = Depends(require_admin_role),
    db: Session = Depends(get_db),
) -> AdminPresenceSnapshotRead:
    snapshot_payload = presence_client.apply_admin_overlay(
        classroom_code=classroomCode,
        payload=payload.model_dump(by_alias=True),
    )
    return AdminPresenceSnapshotRead(**map_presence_snapshot(snapshot_payload, db))


@app.post("/api/admin/presence/classrooms/{classroomCode}/dummy-controls/reset", response_model=AdminPresenceSnapshotRead)
def reset_admin_presence_overlay(
    classroomCode: str,
    _: User = Depends(require_admin_role),
    db: Session = Depends(get_db),
) -> AdminPresenceSnapshotRead:
    snapshot_payload = presence_client.reset_admin_overlay(classroom_code=classroomCode)
    return AdminPresenceSnapshotRead(**map_presence_snapshot(snapshot_payload, db))


@app.get("/api/students/{student_id}/devices", response_model=list[DeviceRead])
def get_devices(
    student_id: str,
    current_user: User = Depends(require_authenticated_user),
    db: Session = Depends(get_db),
) -> list[DeviceRead]:
    require_student_self(student_id, current_user)
    return [
        DeviceRead(
            id=device.id,
            student_id=student_id,
            label=device.label,
            mac_address=device.mac_address,
            status=device.status,
            created_at=device.created_at,
        )
        for device in list_devices(db, student_id)
    ]


@app.post("/api/students/{student_id}/devices", response_model=DeviceRead, status_code=status.HTTP_201_CREATED)
def add_device(
    student_id: str,
    payload: DeviceCreate,
    current_user: User = Depends(require_authenticated_user),
    db: Session = Depends(get_db),
) -> DeviceRead:
    require_student_self(student_id, current_user)
    device = create_device(db, student_id, payload)
    return DeviceRead(
        id=device.id,
        student_id=student_id,
        label=device.label,
        mac_address=device.mac_address,
        status=device.status,
        created_at=device.created_at,
    )


@app.delete("/api/students/{student_id}/devices/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_device(
    student_id: str,
    device_id: int,
    current_user: User = Depends(require_authenticated_user),
    db: Session = Depends(get_db),
) -> Response:
    require_student_self(student_id, current_user)
    delete_device(db, student_id, device_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post(
    "/api/attendance/eligibility",
    response_model=AttendanceEligibilityResponse,
)
def attendance_eligibility(
    payload: AttendanceEligibilityRequest,
    current_user: User = Depends(require_authenticated_user),
    db: Session = Depends(get_db),
) -> AttendanceEligibilityResponse:
    require_student_self(payload.student_id, current_user)
    result = check_attendance_eligibility(
        db=db,
        presence_client=presence_client,
        student_id=payload.student_id,
        course_id=payload.course_code,
        classroom_id=None,
        purpose="attendance",
    )
    return AttendanceEligibilityResponse(**result)
