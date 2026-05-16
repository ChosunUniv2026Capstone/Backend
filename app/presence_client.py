from typing import Any

from fastapi import HTTPException
import httpx


def _raise_presence_http_error(exc: httpx.HTTPStatusError) -> None:
    response = exc.response
    try:
        payload = response.json()
    except ValueError:
        payload = {"message": response.text or "presence service request failed"}
    if isinstance(payload, dict) and "detail" in payload:
        detail = payload["detail"]
    else:
        detail = payload
    if isinstance(detail, dict):
        code = detail.get("code") or detail.get("reason_code") or "PRESENCE_SERVICE_ERROR"
        message = detail.get("message") or "presence service request failed"
        details = detail.get("details", {})
    else:
        code = "PRESENCE_SERVICE_ERROR"
        message = str(detail) if detail else "presence service request failed"
        details = {}
    raise HTTPException(
        status_code=response.status_code,
        detail={"code": code, "message": message, "details": details},
    ) from exc


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
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            _raise_presence_http_error(exc)
        return response.json()

    def get_admin_snapshot(self, *, classroom_code: str, refresh: bool = False) -> dict[str, Any]:
        response = httpx.get(
            f"{self._base_url}/admin/dummy/classrooms/{classroom_code}/snapshot",
            params={"refresh": "true"} if refresh else None,
            timeout=10.0,
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            _raise_presence_http_error(exc)
        return response.json()

    def apply_admin_overlay(self, *, classroom_code: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = httpx.post(
            f"{self._base_url}/admin/dummy/classrooms/{classroom_code}/overlay",
            json=payload,
            timeout=10.0,
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            _raise_presence_http_error(exc)
        return response.json()

    def reset_admin_overlay(self, *, classroom_code: str) -> dict[str, Any]:
        response = httpx.post(
            f"{self._base_url}/admin/dummy/classrooms/{classroom_code}/overlay/reset",
            timeout=10.0,
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            _raise_presence_http_error(exc)
        return response.json()
