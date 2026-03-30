import pytest
from fastapi import HTTPException

from app.services import MAX_DEVICES_PER_STUDENT, normalize_mac


def test_normalize_mac_lowercases_valid_addresses() -> None:
    assert normalize_mac("AA:BB:CC:DD:EE:FF") == "aa:bb:cc:dd:ee:ff"


def test_normalize_mac_rejects_invalid_addresses() -> None:
    with pytest.raises(HTTPException):
        normalize_mac("invalid-mac")


def test_device_limit_constant_is_five() -> None:
    assert MAX_DEVICES_PER_STUDENT == 5
