from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.auth import (
    RefreshRotationBundle,
    auth_error,
    create_login_session,
    revoke_refresh_session,
    rotate_refresh_session,
    verify_access_token,
)
from app.config import get_settings
from app.db import SessionLocal, get_db
from app.attendance import (
    attendance_event_payload,
    build_attendance_report,
    build_attendance_timeline,
    close_attendance_session,
    expire_stale_attendance_sessions,
    get_attendance_session_roster,
    get_attendance_slot_roster_preview,
    get_course_by_code,
    get_owned_course,
    get_student_user,
    list_attendance_history,
    list_student_active_attendance_sessions,
    open_attendance_sessions_batch,
    ensure_student_enrolled,
    student_attendance_check_in,
    update_attendance_session_record,
)
from app.models import Course, CourseEnrollment, RegisteredDevice, User
from app.presence_client import PresenceClient
from app.schemas import (
    AdminClassroomNetworkThresholdUpdate,
    AdminPresenceSnapshotMutationRequest,
    AdminPresenceSnapshotRead,
    AttendanceRecordUpdateRequest,
    AttendanceEligibilityRequest,
    AttendanceEligibilityResponse,
    AttendanceSessionBatchRequest,
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


def _cors_origins() -> list[str]:
    if settings.cors_origins == "*":
        return [origin.strip() for origin in settings.local_cors_origins.split(",") if origin.strip()]
    return [origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()]


app = FastAPI(title="Backend", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class AttendanceRealtimeBroker:
    def __init__(self) -> None:
        self._connections: dict[str, set[WebSocket]] = defaultdict(set)
        self._connection_meta: dict[WebSocket, dict[str, Any]] = {}

    async def connect(self, course_code: str, websocket: WebSocket, meta: dict[str, Any]) -> None:
        await websocket.accept()
        self._connections[course_code].add(websocket)
        self._connection_meta[websocket] = meta

    def disconnect(self, course_code: str, websocket: WebSocket) -> None:
        if course_code in self._connections:
            self._connections[course_code].discard(websocket)
            if not self._connections[course_code]:
                self._connections.pop(course_code, None)
        self._connection_meta.pop(websocket, None)

    async def publish(self, course_code: str, payload: dict[str, Any]) -> None:
        sockets = list(self._connections.get(course_code, set()))
        stale: list[WebSocket] = []
        for websocket in sockets:
            meta = self._connection_meta.get(websocket, {})
            changed_student_id = payload.get("changed_payload", {}).get("student_id")
            if meta.get("role") == "student" and changed_student_id and meta.get("login_id") != changed_student_id:
                continue
            if meta.get("role") == "admin" and meta.get("view") != "report":
                continue
            try:
                await websocket.send_json(payload)
            except RuntimeError:
                stale.append(websocket)
        for websocket in stale:
            self.disconnect(course_code, websocket)


attendance_broker = AttendanceRealtimeBroker()


def api_error(status_code: int, code: str, message: str, details: dict | None = None) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={
            "code": code,
            "message": message,
            "details": details or {},
        },
    )


def success_payload(
    data: dict[str, Any],
    *,
    message: str = "ok",
    meta: dict[str, Any] | None = None,
    compatibility: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "success": True,
        "data": data,
        "message": message,
        "meta": meta or {},
    }
    if compatibility:
        payload.update(compatibility)
    return payload


def error_payload(status_code: int, code: str, message: str, details: dict[str, Any] | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "success": False,
            "error": {
                "code": code,
                "message": message,
                "details": details or {},
            },
        },
    )


def error_response_from_exception(exc: HTTPException) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, dict) else {}
    return error_payload(
        exc.status_code,
        detail.get("code", "REQUEST_FAILED"),
        detail.get("message", "request failed"),
        detail.get("details", {}),
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


def serialize_auth_user(user: User) -> AuthUser:
    return AuthUser(
        id=user.id,
        role=user.role,
        login_id=get_user_login_id(user),
        name=user.name,
    )


def build_route_access(db: Session, user: User) -> dict[str, Any]:
    route_access: dict[str, Any] = {
        "student_course_codes": [],
        "professor_course_codes": [],
        "can_view_admin_report": user.role == "admin",
    }
    login_id = get_user_login_id(user)
    if user.role == "student":
        route_access["student_course_codes"] = [course["course_code"] for course in list_student_courses(db, login_id)]
    elif user.role == "professor":
        route_access["professor_course_codes"] = [course["course_code"] for course in list_professor_courses(db, login_id)]
    return route_access


def set_refresh_cookie(response: Response, refresh_token: str, refresh_expires_at: datetime) -> None:
    response.set_cookie(
        key=settings.refresh_cookie_name,
        value=refresh_token,
        httponly=True,
        secure=settings.refresh_cookie_secure,
        samesite=settings.refresh_cookie_samesite,
        path=settings.refresh_cookie_path,
        domain=settings.refresh_cookie_domain,
        max_age=settings.refresh_token_ttl_seconds,
        expires=refresh_expires_at,
    )


def set_access_cookie(response: Response, access_token: str, access_expires_at: datetime | None) -> None:
    response.set_cookie(
        key=settings.access_cookie_name,
        value=access_token,
        httponly=True,
        secure=settings.access_cookie_secure,
        samesite=settings.access_cookie_samesite,
        path=settings.access_cookie_path,
        domain=settings.access_cookie_domain,
        max_age=settings.access_token_ttl_seconds,
        expires=access_expires_at,
    )


def clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        key=settings.refresh_cookie_name,
        path=settings.refresh_cookie_path,
        domain=settings.refresh_cookie_domain,
    )


def clear_access_cookie(response: Response) -> None:
    response.delete_cookie(
        key=settings.access_cookie_name,
        path=settings.access_cookie_path,
        domain=settings.access_cookie_domain,
    )


def auth_success_response(
    data: dict[str, Any],
    *,
    message: str = "ok",
    meta: dict[str, Any] | None = None,
    compatibility: dict[str, Any] | None = None,
    access_token: str | None = None,
    access_expires_at: datetime | None = None,
    refresh_token: str | None = None,
    refresh_expires_at: datetime | None = None,
) -> JSONResponse:
    response = JSONResponse(
        content=success_payload(
            data,
            message=message,
            meta=meta,
            compatibility=compatibility,
        )
    )
    if access_token and access_expires_at:
        set_access_cookie(response, access_token, access_expires_at)
    if refresh_token and refresh_expires_at:
        set_refresh_cookie(response, refresh_token, refresh_expires_at)
    return response


def build_auth_session_payload(db: Session, bundle: RefreshRotationBundle | None, user: User, access_token: str, access_expires_at: datetime | None) -> dict[str, Any]:
    payload = {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_at": access_expires_at.isoformat() if access_expires_at else None,
        "user": serialize_auth_user(user).model_dump(),
        "route_access": build_route_access(db, user),
    }
    if bundle is not None:
        payload["refresh_expires_at"] = bundle.refresh_expires_at.isoformat()
    return payload


def parse_bearer_login_id(authorization: str | None) -> str:
    if not authorization:
        raise api_error(
            status.HTTP_401_UNAUTHORIZED,
            "UNAUTHENTICATED",
            "authentication is required",
        )

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise api_error(
            status.HTTP_401_UNAUTHORIZED,
            "UNAUTHENTICATED",
            "invalid access token",
        )
    return verify_access_token(token).login_id


def require_authenticated_user(
    request: Request,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    try:
        if authorization:
            login_id = parse_bearer_login_id(authorization)
        else:
            raw_access_cookie = request.cookies.get(settings.access_cookie_name)
            if not raw_access_cookie:
                raise api_error(
                    status.HTTP_401_UNAUTHORIZED,
                    "UNAUTHENTICATED",
                    "authentication is required",
                )
            login_id = verify_access_token(raw_access_cookie).login_id
        return get_user_by_login_id(db, login_id)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        raise api_error(
            status.HTTP_401_UNAUTHORIZED,
            detail.get("code", "UNAUTHENTICATED"),
            detail.get("message", "invalid access token"),
            detail.get("details", {}),
        ) from exc


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


def require_professor_course_ownership(
    professor_id: str,
    course_code: str,
    current_user: User,
    db: Session,
) -> tuple[User, Course]:
    require_professor_self(professor_id, current_user)
    return get_owned_course(db, professor_id, course_code)


def require_student_course_access(
    student_id: str,
    course_code: str,
    current_user: User,
    db: Session,
) -> tuple[User, Course]:
    require_student_self(student_id, current_user)
    student = get_student_user(db, student_id)
    course = get_course_by_code(db, course_code)
    ensure_student_enrolled(db, student.id, course.id, student_id, course_code)
    return student, course


def require_professor_route_bootstrap_access(
    professor_id: str,
    course_code: str,
    current_user: User,
    db: Session,
) -> tuple[User, Course]:
    require_professor_self(professor_id, current_user)
    try:
        return get_owned_course(db, professor_id, course_code)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        raise api_error(
            status.HTTP_403_FORBIDDEN,
            "COURSE_ROUTE_FORBIDDEN",
            "course route is not allowed",
            {"course_code": course_code, "reason": detail.get("code", "FORBIDDEN")},
        ) from exc


def require_student_route_bootstrap_access(
    student_id: str,
    course_code: str,
    current_user: User,
    db: Session,
) -> tuple[User, Course]:
    require_student_self(student_id, current_user)
    student = get_student_user(db, student_id)
    course = get_course_by_code(db, course_code)
    enrolled = db.scalar(
        select(CourseEnrollment.id).where(
            CourseEnrollment.course_id == course.id,
            CourseEnrollment.student_user_id == student.id,
            CourseEnrollment.status == "active",
        )
    )
    if enrolled is None:
        raise api_error(
            status.HTTP_403_FORBIDDEN,
            "COURSE_ROUTE_FORBIDDEN",
            "course route is not allowed",
            {"course_code": course_code, "student_id": student_id},
        )
    return student, course


def require_login_match(requested_login_id: str, current_user: User) -> None:
    if get_user_login_id(current_user) != requested_login_id:
        raise api_error(
            status.HTTP_403_FORBIDDEN,
            "FORBIDDEN",
            "requested user does not match the authenticated user",
            {"requested_login_id": requested_login_id},
        )


def validate_attendance_socket_access(db: Session, user: User, course_code: str, view: str) -> None:
    if user.role == "student":
        student = get_student_user(db, get_user_login_id(user))
        course = get_course_by_code(db, course_code)
        ensure_student_enrolled(db, student.id, course.id, get_user_login_id(user), course_code)
        return

    if user.role == "professor":
        _, _ = get_owned_course(db, get_user_login_id(user), course_code)
        return

    if user.role == "admin" and view == "report":
        return

    raise api_error(status.HTTP_403_FORBIDDEN, "FORBIDDEN", "websocket subscription is not allowed")


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


@app.post("/api/auth/login", response_model=None)
def login(
    payload: AuthLoginRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any] | JSONResponse:
    try:
        user = authenticate_user(db, payload.login_id, payload.password)
        bundle = create_login_session(db, user)
        auth_payload = build_auth_session_payload(
            db,
            bundle,
            user,
            bundle.access_token,
            bundle.access_expires_at,
        )
        return auth_success_response(
            auth_payload,
            meta={
                "refresh_cookie_name": settings.refresh_cookie_name,
                "access_cookie_name": settings.access_cookie_name,
                "legacy_dev_token_enabled": settings.auth_allow_legacy_dev_tokens,
            },
            compatibility={
                "access_token": bundle.access_token,
                "user": auth_payload["user"],
            },
            access_token=bundle.access_token,
            access_expires_at=bundle.access_expires_at,
            refresh_token=bundle.refresh_token,
            refresh_expires_at=bundle.refresh_expires_at,
        )
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        return error_payload(
            exc.status_code,
            detail.get("code", "UNAUTHENTICATED"),
            detail.get("message", "invalid credentials"),
            detail.get("details", {"login_id": payload.login_id}),
        )


@app.post("/api/auth/refresh", response_model=None)
def refresh_auth_session(
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any] | JSONResponse:
    raw_refresh_token = request.cookies.get(settings.refresh_cookie_name)
    if not raw_refresh_token:
        return error_payload(status.HTTP_401_UNAUTHORIZED, "UNAUTHENTICATED", "refresh token is required")
    try:
        bundle = rotate_refresh_session(db, raw_refresh_token)
        return auth_success_response(
            build_auth_session_payload(
                db,
                bundle,
                bundle.user,
                bundle.access_token,
                bundle.access_expires_at,
            ),
            meta={"refreshed": True},
            access_token=bundle.access_token,
            access_expires_at=bundle.access_expires_at,
            refresh_token=bundle.refresh_token,
            refresh_expires_at=bundle.refresh_expires_at,
        )
    except HTTPException as exc:
        error_response = error_response_from_exception(exc)
        clear_access_cookie(error_response)
        clear_refresh_cookie(error_response)
        return error_response


def _bootstrap_auth_session(
    request: Request,
    db: Session,
) -> dict[str, Any] | JSONResponse:
    authorization = request.headers.get("Authorization")
    if authorization:
        try:
            identity = verify_access_token(authorization.partition(" ")[2] if " " in authorization else authorization)
            user = get_user_by_login_id(db, identity.login_id)
            access_token = authorization.partition(" ")[2] if " " in authorization else authorization
            return auth_success_response(
                build_auth_session_payload(db, None, user, access_token, identity.expires_at),
                meta={
                    "restored_via": "access-token",
                    "legacy_dev_token": identity.legacy_dev_token,
                },
            )
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            if detail.get("code") not in {"TOKEN_EXPIRED", "UNAUTHENTICATED"}:
                return error_response_from_exception(exc)

    raw_access_cookie = request.cookies.get(settings.access_cookie_name)
    if raw_access_cookie:
        try:
            identity = verify_access_token(raw_access_cookie)
            user = get_user_by_login_id(db, identity.login_id)
            return auth_success_response(
                build_auth_session_payload(db, None, user, raw_access_cookie, identity.expires_at),
                meta={"restored_via": "access-cookie"},
            )
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            if detail.get("code") not in {"TOKEN_EXPIRED", "UNAUTHENTICATED"}:
                return error_response_from_exception(exc)

    raw_refresh_token = request.cookies.get(settings.refresh_cookie_name)
    if not raw_refresh_token:
        return error_payload(status.HTTP_401_UNAUTHORIZED, "UNAUTHENTICATED", "authentication is required")
    try:
        bundle = rotate_refresh_session(db, raw_refresh_token)
        return auth_success_response(
            build_auth_session_payload(
                db,
                bundle,
                bundle.user,
                bundle.access_token,
                bundle.access_expires_at,
            ),
            meta={"restored_via": "refresh-cookie"},
            access_token=bundle.access_token,
            access_expires_at=bundle.access_expires_at,
            refresh_token=bundle.refresh_token,
            refresh_expires_at=bundle.refresh_expires_at,
        )
    except HTTPException as exc:
        error_response = error_response_from_exception(exc)
        clear_access_cookie(error_response)
        clear_refresh_cookie(error_response)
        return error_response


@app.get("/api/auth/bootstrap", response_model=None)
def bootstrap_auth_session(
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any] | JSONResponse:
    return _bootstrap_auth_session(request, db)


@app.get("/api/auth/me", response_model=None)
def bootstrap_auth_session_alias(
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any] | JSONResponse:
    return _bootstrap_auth_session(request, db)


@app.post("/api/auth/logout")
def logout_auth_session(
    request: Request,
    db: Session = Depends(get_db),
) -> JSONResponse:
    revoke_refresh_session(db, request.cookies.get(settings.refresh_cookie_name))
    response = JSONResponse(content=success_payload({"logged_out": True}, meta={"refresh_cookie_cleared": True}))
    clear_access_cookie(response)
    clear_refresh_cookie(response)
    return response


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


@app.get("/api/students/{student_id}/courses/{course_code}/attendance/bootstrap")
async def student_attendance_bootstrap(
    student_id: str,
    course_code: str,
    current_user: User = Depends(require_authenticated_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    student, course = require_student_route_bootstrap_access(student_id, course_code, current_user, db)
    expired_events = expire_stale_attendance_sessions(db, course_code)
    for event in expired_events:
        await attendance_broker.publish(
            event["course_code"],
            attendance_event_payload(
                event_type=event["event_type"],
                course_code=event["course_code"],
                projection_keys=event.get("projection_keys", [event["projection_key"]]),
                session_ids=[event["session_id"]],
                version=event["version"],
            ),
        )
    return success_payload(
        {
            "user": serialize_auth_user(student).model_dump(),
            "course": CourseRead(
                id=course.id,
                course_code=course.course_code,
                title=course.title,
            ).model_dump(),
            "attendance": list_student_active_attendance_sessions(db, presence_client, student_id, course_code),
        },
        meta={"route": "student-attendance-bootstrap"},
    )


@app.get("/api/professors/{professor_id}/courses/{course_code}/attendance/bootstrap")
async def professor_attendance_bootstrap(
    professor_id: str,
    course_code: str,
    current_user: User = Depends(require_authenticated_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    professor, course = require_professor_route_bootstrap_access(professor_id, course_code, current_user, db)
    expired_events = expire_stale_attendance_sessions(db, course_code)
    for event in expired_events:
        await attendance_broker.publish(
            event["course_code"],
            attendance_event_payload(
                event_type=event["event_type"],
                course_code=event["course_code"],
                projection_keys=event.get("projection_keys", [event["projection_key"]]),
                session_ids=[event["session_id"]],
                version=event["version"],
            ),
        )
    timeline = build_attendance_timeline(db, professor_id, course_code)
    return success_payload(
        {
            "user": serialize_auth_user(professor).model_dump(),
            "course": CourseRead(
                id=course.id,
                course_code=course.course_code,
                title=course.title,
            ).model_dump(),
            "attendance": timeline,
        },
        meta={"route": "professor-attendance-bootstrap"},
    )


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


@app.get("/api/professors/{professor_id}/courses/{course_code}/attendance/timeline")
async def professor_attendance_timeline(
    professor_id: str,
    course_code: str,
    current_user: User = Depends(require_authenticated_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    require_professor_course_ownership(professor_id, course_code, current_user, db)
    expired_events = expire_stale_attendance_sessions(db, course_code)
    for event in expired_events:
        await attendance_broker.publish(
            event["course_code"],
            attendance_event_payload(
                event_type=event["event_type"],
                course_code=event["course_code"],
                projection_keys=event.get("projection_keys", [event["projection_key"]]),
                session_ids=[event["session_id"]],
                version=event["version"],
            ),
        )
    return build_attendance_timeline(db, professor_id, course_code)


@app.get("/api/professors/{professor_id}/courses/{course_code}/attendance/report")
async def professor_attendance_report(
    professor_id: str,
    course_code: str,
    current_user: User = Depends(require_authenticated_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    require_professor_course_ownership(professor_id, course_code, current_user, db)
    expired_events = expire_stale_attendance_sessions(db, course_code)
    for event in expired_events:
        await attendance_broker.publish(
            event["course_code"],
            attendance_event_payload(
                event_type=event["event_type"],
                course_code=event["course_code"],
                projection_keys=event.get("projection_keys", [event["projection_key"]]),
                session_ids=[event["session_id"]],
                version=event["version"],
            ),
        )
    return build_attendance_report(db, professor_id, course_code)


@app.post("/api/professors/{professor_id}/courses/{course_code}/attendance/sessions/batch")
async def professor_open_attendance_sessions_batch(
    professor_id: str,
    course_code: str,
    payload: AttendanceSessionBatchRequest,
    current_user: User = Depends(require_authenticated_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    require_professor_course_ownership(professor_id, course_code, current_user, db)
    result = open_attendance_sessions_batch(
        db,
        professor_id,
        course_code,
        projection_keys=payload.projection_keys,
        mode=payload.mode,
    )
    await attendance_broker.publish(
        course_code,
        attendance_event_payload(
            event_type="attendance.session.batch_applied",
            course_code=course_code,
            projection_keys=result["changed_projection_keys"],
            session_ids=result["changed_session_ids"],
            changed_payload={"results": result["results"], "mode": payload.mode},
        ),
    )
    return result


@app.post("/api/professors/{professor_id}/attendance/sessions/{session_id}/close")
async def professor_close_attendance(
    professor_id: str,
    session_id: int,
    current_user: User = Depends(require_authenticated_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    require_professor_self(professor_id, current_user)
    result = close_attendance_session(db, professor_id, session_id)
    if "course_code" in result:
        await attendance_broker.publish(
            result["course_code"],
            attendance_event_payload(
                event_type="attendance.session.closed",
                course_code=result["course_code"],
                projection_keys=result.get("projection_keys", [result["projection_key"]]),
                session_ids=[result["session_id"]],
                version=result["version"],
            ),
        )
    return result


@app.get("/api/professors/{professor_id}/attendance/sessions/{session_id}/roster")
def professor_attendance_roster(
    professor_id: str,
    session_id: int,
    current_user: User = Depends(require_authenticated_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    require_professor_self(professor_id, current_user)
    return get_attendance_session_roster(db, professor_id, session_id)


@app.get("/api/professors/{professor_id}/courses/{course_code}/attendance/slot-roster")
def professor_attendance_slot_roster(
    professor_id: str,
    course_code: str,
    projection_key: str = Query(...),
    current_user: User = Depends(require_authenticated_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    require_professor_course_ownership(professor_id, course_code, current_user, db)
    return get_attendance_slot_roster_preview(db, professor_id, course_code, projection_key)


@app.patch("/api/professors/{professor_id}/attendance/sessions/{session_id}/students/{student_id}")
async def professor_update_attendance_record(
    professor_id: str,
    session_id: int,
    student_id: str,
    payload: AttendanceRecordUpdateRequest,
    current_user: User = Depends(require_authenticated_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    require_professor_self(professor_id, current_user)
    result = update_attendance_session_record(
        db,
        professor_id,
        session_id,
        student_id,
        payload.status,
        payload.reason,
        payload.projection_key,
    )
    if result.get("changed", True):
        await attendance_broker.publish(
            result["course_code"],
            attendance_event_payload(
                event_type="attendance.record.updated",
                course_code=result["course_code"],
                projection_keys=result.get("projection_keys", [result["projection_key"]]),
                session_ids=[result["session_id"]],
                version=result["version"],
                changed_payload={
                    "student_id": result["student_id"],
                    "new_status": result["new_status"],
                    "projection_keys": result.get("projection_keys", []),
                },
            ),
        )
    return result


@app.get("/api/professors/{professor_id}/courses/{course_code}/attendance/students/{student_id}/history")
def professor_attendance_student_history(
    professor_id: str,
    course_code: str,
    student_id: str,
    current_user: User = Depends(require_authenticated_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    require_professor_course_ownership(professor_id, course_code, current_user, db)
    return list_attendance_history(db, professor_id, course_code, student_id)


@app.get("/api/students/{student_id}/courses/{course_code}/attendance/active-sessions")
async def student_active_attendance_sessions(
    student_id: str,
    course_code: str,
    current_user: User = Depends(require_authenticated_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    require_student_course_access(student_id, course_code, current_user, db)
    expired_events = expire_stale_attendance_sessions(db, course_code)
    for event in expired_events:
        await attendance_broker.publish(
            event["course_code"],
            attendance_event_payload(
                event_type=event["event_type"],
                course_code=event["course_code"],
                projection_keys=event.get("projection_keys", [event["projection_key"]]),
                session_ids=[event["session_id"]],
                version=event["version"],
            ),
        )
    return list_student_active_attendance_sessions(db, presence_client, student_id, course_code)


@app.post("/api/students/{student_id}/attendance/sessions/{session_id}/check-in")
async def student_attendance_check_in_endpoint(
    student_id: str,
    session_id: int,
    current_user: User = Depends(require_authenticated_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    require_student_self(student_id, current_user)
    result = student_attendance_check_in(db, presence_client, student_id, session_id)
    await attendance_broker.publish(
        result["course_code"],
        attendance_event_payload(
            event_type="attendance.student.checked_in",
            course_code=result["course_code"],
            projection_keys=result.get("projection_keys", [result["projection_key"]]),
            session_ids=[result["session_id"]],
            version=result["version"],
            changed_payload={
                "student_id": result["student_id"],
                "status": result["status"],
                "idempotent": result["idempotent"],
                "changed_count": result.get("changed_count"),
                "already_present_count": result.get("already_present_count"),
                "rejected_count": result.get("rejected_count"),
                "projection_keys": result.get("projection_keys", []),
            },
        ),
    )
    return result


@app.websocket("/ws/attendance")
async def attendance_websocket(
    websocket: WebSocket,
    token: str | None = Query(default=None),
    courseCode: str = Query(...),
    view: str = Query(default="professor"),
) -> None:
    db = SessionLocal()
    try:
        if token:
            login_id = parse_bearer_login_id(f"Bearer {token}")
            user = get_user_by_login_id(db, login_id)
        else:
            raw_access_cookie = websocket.cookies.get(settings.access_cookie_name)
            if not raw_access_cookie:
                raise api_error(status.HTTP_401_UNAUTHORIZED, "UNAUTHENTICATED", "authentication is required")
            identity = verify_access_token(raw_access_cookie)
            login_id = identity.login_id
            user = get_user_by_login_id(db, login_id)
        validate_attendance_socket_access(db, user, courseCode, view)
        await attendance_broker.connect(
            courseCode,
            websocket,
            {"login_id": login_id, "role": user.role, "view": view, "course_code": courseCode},
        )
        if user.role == "student":
            bootstrap_data = list_student_active_attendance_sessions(db, presence_client, login_id, courseCode)
        elif user.role == "admin":
            course = get_course_by_code(db, courseCode)
            owner = db.scalar(select(User).where(User.id == course.professor_user_id))
            bootstrap_data = build_attendance_timeline(db, owner.professor_id if owner and owner.professor_id else "", courseCode)
        else:
            bootstrap_data = build_attendance_timeline(db, user.professor_id or login_id, courseCode)
        await websocket.send_json(
            attendance_event_payload(
                event_type="attendance.bootstrap",
                course_code=courseCode,
                changed_payload={"view": view, "data": bootstrap_data},
            )
        )
        while True:
            await websocket.receive_text()
    except HTTPException:
        await websocket.close(code=1008)
    except WebSocketDisconnect:
        pass
    finally:
        attendance_broker.disconnect(courseCode, websocket)
        db.close()
