from typing import Any

from fastapi import HTTPException
import httpx


PRESENCE_DEPENDENCY_UNAVAILABLE_CODES = {
    "PRESENCE_SERVICE_UNAVAILABLE",
    "COLLECTOR_REGISTRY_UNAVAILABLE",
}


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


def _raise_presence_request_error(exc: httpx.RequestError) -> None:
    raise HTTPException(
        status_code=503,
        detail={
            "code": "PRESENCE_SERVICE_UNAVAILABLE",
            "message": "presence service is unavailable",
            "details": {"error": str(exc)},
        },
    ) from exc


def is_presence_dependency_unavailable(exc: HTTPException) -> bool:
    if not isinstance(exc.detail, dict):
        return False
    return str(exc.detail.get("code")) in PRESENCE_DEPENDENCY_UNAVAILABLE_CODES


def presence_dependency_unavailable_result(
    exc: HTTPException,
    *,
    classroom_id: str | None,
) -> dict[str, Any]:
    detail = exc.detail if isinstance(exc.detail, dict) else {}
    upstream_code = str(detail.get("code") or "PRESENCE_SERVICE_UNAVAILABLE")
    message = str(detail.get("message") or "presence service is unavailable")
    details = detail.get("details") if isinstance(detail.get("details"), dict) else {}
    return {
        "eligible": False,
        "reason_code": "PRESENCE_SERVICE_UNAVAILABLE",
        "matched_device_mac": None,
        "observed_at": None,
        "snapshot_age_seconds": None,
        "evidence": {
            "classroomId": classroom_id,
            "dependencyUnavailable": True,
            "upstreamStatusCode": exc.status_code,
            "upstreamCode": upstream_code,
            "upstreamMessage": message,
            "upstreamDetails": details,
        },
    }


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
        try:
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
        except httpx.RequestError as exc:
            _raise_presence_request_error(exc)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            _raise_presence_http_error(exc)
        return response.json()

    def get_admin_snapshot(self, *, classroom_code: str, refresh: bool = False, source: str = "auto") -> dict[str, Any]:
        params: dict[str, str] = {}
        if refresh:
            params["refresh"] = "true"
        if source != "auto":
            params["source"] = source
        try:
            response = httpx.get(
                f"{self._base_url}/admin/dummy/classrooms/{classroom_code}/snapshot",
                params=params or None,
                timeout=10.0,
            )
        except httpx.RequestError as exc:
            _raise_presence_request_error(exc)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            _raise_presence_http_error(exc)
        return response.json()

    def apply_admin_overlay(self, *, classroom_code: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = httpx.post(
                f"{self._base_url}/admin/dummy/classrooms/{classroom_code}/overlay",
                json=payload,
                timeout=10.0,
            )
        except httpx.RequestError as exc:
            _raise_presence_request_error(exc)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            _raise_presence_http_error(exc)
        return response.json()

    def reset_admin_overlay(self, *, classroom_code: str) -> dict[str, Any]:
        try:
            response = httpx.post(
                f"{self._base_url}/admin/dummy/classrooms/{classroom_code}/overlay/reset",
                timeout=10.0,
            )
        except httpx.RequestError as exc:
            _raise_presence_request_error(exc)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            _raise_presence_http_error(exc)
        return response.json()
