from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import RefreshSession, User
from app.services import get_user_by_login_id, get_user_login_id

JWT_ALGORITHM = "HS256"


@dataclass(frozen=True)
class AccessTokenBundle:
    token: str
    expires_at: datetime


@dataclass(frozen=True)
class RefreshRotationBundle:
    user: User
    access_token: str
    access_expires_at: datetime
    refresh_token: str
    refresh_expires_at: datetime


@dataclass(frozen=True)
class AuthIdentity:
    login_id: str
    role: str | None
    user_id: int | None
    expires_at: datetime | None
    legacy_dev_token: bool = False


def auth_error(
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={
            "code": code,
            "message": message,
            "details": details or {},
        },
    )


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(raw: str) -> bytes:
    padding = "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode(raw + padding)


def _json_dumps(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _sign(signing_input: bytes, secret: str) -> str:
    signature = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return _b64url_encode(signature)


def _encode_jwt(payload: dict[str, Any], secret: str) -> str:
    header = {"alg": JWT_ALGORITHM, "typ": "JWT"}
    encoded_header = _b64url_encode(_json_dumps(header))
    encoded_payload = _b64url_encode(_json_dumps(payload))
    signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
    return f"{encoded_header}.{encoded_payload}.{_sign(signing_input, secret)}"


def _decode_jwt(
    token: str,
    *,
    expected_type: str,
    allow_expired: bool = False,
) -> dict[str, Any]:
    settings = get_settings()
    try:
        encoded_header, encoded_payload, encoded_signature = token.split(".")
    except ValueError as exc:
        raise auth_error(status.HTTP_401_UNAUTHORIZED, "UNAUTHENTICATED", "invalid token format") from exc

    signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
    expected_signature = _sign(signing_input, settings.jwt_secret)
    if not hmac.compare_digest(encoded_signature, expected_signature):
        raise auth_error(status.HTTP_401_UNAUTHORIZED, "UNAUTHENTICATED", "invalid token signature")

    try:
        header = json.loads(_b64url_decode(encoded_header))
        payload = json.loads(_b64url_decode(encoded_payload))
    except (ValueError, json.JSONDecodeError) as exc:
        raise auth_error(status.HTTP_401_UNAUTHORIZED, "UNAUTHENTICATED", "invalid token payload") from exc

    if header.get("alg") != JWT_ALGORITHM or payload.get("typ") != expected_type:
        raise auth_error(status.HTTP_401_UNAUTHORIZED, "UNAUTHENTICATED", "invalid token type")

    exp = payload.get("exp")
    if not isinstance(exp, int):
        raise auth_error(status.HTTP_401_UNAUTHORIZED, "UNAUTHENTICATED", "missing token expiry")
    if not allow_expired and datetime.fromtimestamp(exp, tz=UTC) <= _utcnow():
        raise auth_error(status.HTTP_401_UNAUTHORIZED, "TOKEN_EXPIRED", "token has expired")
    return payload


def _hash_token_id(token_id: str) -> str:
    return hashlib.sha256(token_id.encode("utf-8")).hexdigest()


def _coerce_naive_utc(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None)


def _build_access_payload(user: User, expires_at: datetime) -> dict[str, Any]:
    login_id = get_user_login_id(user)
    issued_at = int(_utcnow().timestamp())
    return {
        "sub": login_id,
        "uid": user.id,
        "role": user.role,
        "typ": "access",
        "iat": issued_at,
        "exp": int(expires_at.timestamp()),
    }


def _build_refresh_payload(user: User, session_key: str, token_id: str, expires_at: datetime) -> dict[str, Any]:
    login_id = get_user_login_id(user)
    issued_at = int(_utcnow().timestamp())
    return {
        "sub": login_id,
        "uid": user.id,
        "role": user.role,
        "sid": session_key,
        "jti": token_id,
        "typ": "refresh",
        "iat": issued_at,
        "exp": int(expires_at.timestamp()),
    }


def issue_access_token(user: User, *, ttl_seconds: int | None = None) -> AccessTokenBundle:
    settings = get_settings()
    expires_at = _utcnow() + timedelta(seconds=ttl_seconds or settings.access_token_ttl_seconds)
    payload = _build_access_payload(user, expires_at)
    return AccessTokenBundle(token=_encode_jwt(payload, settings.jwt_secret), expires_at=expires_at)


def verify_access_token(raw_token: str) -> AuthIdentity:
    settings = get_settings()
    if settings.auth_allow_legacy_dev_tokens and raw_token.startswith("dev-token:"):
        login_id = raw_token.removeprefix("dev-token:").strip()
        if not login_id:
            raise auth_error(status.HTTP_401_UNAUTHORIZED, "UNAUTHENTICATED", "invalid access token")
        return AuthIdentity(
            login_id=login_id,
            role=None,
            user_id=None,
            expires_at=None,
            legacy_dev_token=True,
        )

    payload = _decode_jwt(raw_token, expected_type="access")
    login_id = payload.get("sub")
    if not isinstance(login_id, str) or not login_id:
        raise auth_error(status.HTTP_401_UNAUTHORIZED, "UNAUTHENTICATED", "invalid access token subject")
    role = payload.get("role")
    user_id = payload.get("uid")
    exp = payload.get("exp")
    return AuthIdentity(
        login_id=login_id,
        role=role if isinstance(role, str) else None,
        user_id=user_id if isinstance(user_id, int) else None,
        expires_at=datetime.fromtimestamp(exp, tz=UTC) if isinstance(exp, int) else None,
        legacy_dev_token=False,
    )


def create_login_session(db: Session, user: User) -> RefreshRotationBundle:
    settings = get_settings()
    access_bundle = issue_access_token(user)
    refresh_expires_at = _utcnow() + timedelta(seconds=settings.refresh_token_ttl_seconds)
    session_key = secrets.token_urlsafe(24)
    token_id = secrets.token_urlsafe(24)
    db.add(
        RefreshSession(
            session_key=session_key,
            user_id=user.id,
            current_token_hash=_hash_token_id(token_id),
            expires_at=_coerce_naive_utc(refresh_expires_at),
        )
    )
    db.commit()
    refresh_token = _encode_jwt(
        _build_refresh_payload(user, session_key, token_id, refresh_expires_at),
        settings.jwt_secret,
    )
    return RefreshRotationBundle(
        user=user,
        access_token=access_bundle.token,
        access_expires_at=access_bundle.expires_at,
        refresh_token=refresh_token,
        refresh_expires_at=refresh_expires_at,
    )


def rotate_refresh_session(db: Session, raw_refresh_token: str) -> RefreshRotationBundle:
    payload = _decode_jwt(raw_refresh_token, expected_type="refresh")
    session_key = payload.get("sid")
    token_id = payload.get("jti")
    login_id = payload.get("sub")
    if not isinstance(session_key, str) or not isinstance(token_id, str) or not isinstance(login_id, str):
        raise auth_error(status.HTTP_401_UNAUTHORIZED, "UNAUTHENTICATED", "invalid refresh token payload")

    session = db.scalar(select(RefreshSession).where(RefreshSession.session_key == session_key))
    if session is None:
        raise auth_error(status.HTTP_401_UNAUTHORIZED, "TOKEN_REVOKED", "refresh session is not active")

    now = _utcnow()
    if session.revoked_at is not None:
        raise auth_error(status.HTTP_401_UNAUTHORIZED, "TOKEN_REVOKED", "refresh session is not active")
    if session.expires_at.replace(tzinfo=UTC) <= now:
        session.revoked_at = _coerce_naive_utc(now)
        db.commit()
        raise auth_error(status.HTTP_401_UNAUTHORIZED, "TOKEN_EXPIRED", "refresh token has expired")

    if session.current_token_hash != _hash_token_id(token_id):
        session.revoked_at = _coerce_naive_utc(now)
        session.replay_detected_at = _coerce_naive_utc(now)
        db.commit()
        raise auth_error(status.HTTP_401_UNAUTHORIZED, "REFRESH_REPLAY_DETECTED", "refresh token replay detected")

    user = get_user_by_login_id(db, login_id)
    access_bundle = issue_access_token(user)
    new_refresh_expires_at = now + timedelta(seconds=get_settings().refresh_token_ttl_seconds)
    new_token_id = secrets.token_urlsafe(24)
    session.current_token_hash = _hash_token_id(new_token_id)
    session.expires_at = _coerce_naive_utc(new_refresh_expires_at)
    session.last_rotated_at = _coerce_naive_utc(now)
    db.commit()

    new_refresh_token = _encode_jwt(
        _build_refresh_payload(user, session.session_key, new_token_id, new_refresh_expires_at),
        get_settings().jwt_secret,
    )
    return RefreshRotationBundle(
        user=user,
        access_token=access_bundle.token,
        access_expires_at=access_bundle.expires_at,
        refresh_token=new_refresh_token,
        refresh_expires_at=new_refresh_expires_at,
    )


def revoke_refresh_session(db: Session, raw_refresh_token: str | None) -> None:
    if not raw_refresh_token:
        return
    try:
        payload = _decode_jwt(raw_refresh_token, expected_type="refresh", allow_expired=True)
    except HTTPException:
        return

    session_key = payload.get("sid")
    if not isinstance(session_key, str):
        return

    session = db.scalar(select(RefreshSession).where(RefreshSession.session_key == session_key))
    if session is None or session.revoked_at is not None:
        return

    session.revoked_at = _coerce_naive_utc(_utcnow())
    db.commit()
