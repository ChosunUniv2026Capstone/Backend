from typing import Any

import httpx


class PresenceClient:
    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")

    def check_eligibility(
        self,
        *,
        student_id: str,
        course_id: str,
        classroom_id: str,
        purpose: str,
        registered_devices: list[dict[str, str]],
    ) -> dict[str, Any]:
        response = httpx.post(
            f"{self._base_url}/eligibility/check",
            json={
                "studentId": student_id,
                "courseId": course_id,
                "classroomId": classroom_id,
                "purpose": purpose,
                "registeredDevices": registered_devices,
            },
            timeout=10.0,
        )
        response.raise_for_status()
        return response.json()
