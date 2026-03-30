from fastapi import Depends, FastAPI, Response, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.presence_client import PresenceClient
from app.schemas import (
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
    UserSummary,
)
from app.services import (
    authenticate_user,
    check_attendance_eligibility,
    create_device,
    delete_device,
    get_user_login_id,
    list_classroom_networks,
    list_classrooms,
    list_notices,
    list_professor_courses,
    list_student_courses,
    list_users,
    list_devices,
    create_notice,
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
def get_student_courses(student_id: str, db: Session = Depends(get_db)) -> list[CourseRead]:
    return [CourseRead(**course) for course in list_student_courses(db, student_id)]


@app.get("/api/professors/{professor_id}/courses", response_model=list[CourseRead])
def get_professor_courses(professor_id: str, db: Session = Depends(get_db)) -> list[CourseRead]:
    return [CourseRead(**course) for course in list_professor_courses(db, professor_id)]


@app.get("/api/notices/{login_id}", response_model=list[NoticeRead])
def get_notices(login_id: str, db: Session = Depends(get_db)) -> list[NoticeRead]:
    return [NoticeRead(**notice) for notice in list_notices(db, login_id)]


@app.post("/api/professors/{professor_id}/notices", response_model=NoticeRead, status_code=status.HTTP_201_CREATED)
def add_notice(professor_id: str, payload: NoticeCreate, db: Session = Depends(get_db)) -> NoticeRead:
    notice = create_notice(db, professor_id, payload.title, payload.body, payload.course_code)
    notices = list_notices(db, professor_id)
    created = next(item for item in notices if item["id"] == notice.id)
    return NoticeRead(**created)


@app.get("/api/admin/users", response_model=list[UserSummary])
def get_users(db: Session = Depends(get_db)) -> list[UserSummary]:
    return [UserSummary(**user) for user in list_users(db)]


@app.get("/api/admin/classrooms", response_model=list[ClassroomRead])
def get_classrooms(db: Session = Depends(get_db)) -> list[ClassroomRead]:
    return [ClassroomRead(**classroom) for classroom in list_classrooms(db)]


@app.get("/api/admin/classroom-networks", response_model=list[ClassroomNetworkRead])
def get_classroom_networks(db: Session = Depends(get_db)) -> list[ClassroomNetworkRead]:
    return [ClassroomNetworkRead(**network) for network in list_classroom_networks(db)]


@app.get("/api/students/{student_id}/devices", response_model=list[DeviceRead])
def get_devices(student_id: str, db: Session = Depends(get_db)) -> list[DeviceRead]:
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
def add_device(student_id: str, payload: DeviceCreate, db: Session = Depends(get_db)) -> DeviceRead:
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
def remove_device(student_id: str, device_id: int, db: Session = Depends(get_db)) -> Response:
    delete_device(db, student_id, device_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post(
    "/api/attendance/eligibility",
    response_model=AttendanceEligibilityResponse,
)
def attendance_eligibility(
    payload: AttendanceEligibilityRequest,
    db: Session = Depends(get_db),
) -> AttendanceEligibilityResponse:
    result = check_attendance_eligibility(
        db=db,
        presence_client=presence_client,
        student_id=payload.student_id,
        course_id=payload.course_id,
        classroom_id=payload.classroom_id,
        purpose=payload.purpose,
    )
    return AttendanceEligibilityResponse(**result)
