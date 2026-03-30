from fastapi import HTTPException

from app.services import normalize_mac


def test_normalize_mac_lowercases_valid_addresses():
    assert normalize_mac("AA:BB:CC:DD:EE:FF") == "aa:bb:cc:dd:ee:ff"


def test_normalize_mac_rejects_invalid_addresses():
    try:
        normalize_mac("invalid")
    except HTTPException as exc:
        assert exc.status_code == 400
    else:
        raise AssertionError("normalize_mac should reject invalid values")
