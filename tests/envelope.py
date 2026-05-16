from __future__ import annotations

from collections.abc import Iterator
from typing import Any


class ApiJson:
    def __init__(self, payload: Any) -> None:
        self._payload = payload
        self._success = isinstance(payload, dict) and payload.get("success") is True and "data" in payload
        self._failure = isinstance(payload, dict) and payload.get("success") is False and isinstance(payload.get("error"), dict)

    @property
    def _data(self) -> Any:
        return self._payload["data"] if self._success else self._payload

    @property
    def _error(self) -> dict[str, Any]:
        return self._payload["error"] if self._failure else {}

    def __getitem__(self, key: Any) -> Any:
        if self._success:
            if key in {"success", "data", "message", "meta"}:
                return self._payload[key]
            return self._data[key]
        if self._failure:
            if key == "detail":
                return self._error
            if key in self._payload:
                return self._payload[key]
            return self._error[key]
        return self._payload[key]

    def __iter__(self) -> Iterator[Any]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __contains__(self, item: Any) -> bool:
        return item in self._data


def api_json(response: Any) -> ApiJson:
    return ApiJson(response.json())
