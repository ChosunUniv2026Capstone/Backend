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
        classroom_networks: list[dict[str, Any]],
        registered_devices: list[dict[str, str]],
    ) -> dict[str, Any]:
        response = httpx.post(
            f"{self._base_url}/eligibility/check",
            json={
                "studentId": student_id,
                "courseId": course_id,
                "classroomId": classroom_id,
                "purpose": purpose,
                "classroomNetworks": classroom_networks,
                "registeredDevices": registered_devices,
            },
            timeout=10.0,
        )
        response.raise_for_status()
        return response.json()

    def get_admin_snapshot(self, *, classroom_code: str) -> dict[str, Any]:
        response = httpx.get(
            f"{self._base_url}/admin/dummy/classrooms/{classroom_code}/snapshot",
            timeout=10.0,
        )
        response.raise_for_status()
        return response.json()

    def apply_admin_overlay(self, *, classroom_code: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = httpx.post(
            f"{self._base_url}/admin/dummy/classrooms/{classroom_code}/overlay",
            json=payload,
            timeout=10.0,
        )
        response.raise_for_status()
        return response.json()

    def reset_admin_overlay(self, *, classroom_code: str) -> dict[str, Any]:
        response = httpx.post(
            f"{self._base_url}/admin/dummy/classrooms/{classroom_code}/overlay/reset",
            timeout=10.0,
        )
        response.raise_for_status()
        return response.json()
