"""Password hashing and authenticated, expiring, revocable browser sessions."""

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections import OrderedDict, deque

from fastapi import HTTPException, Request, Response

from app.core.config import settings
from app.features.users.model import User

COOKIE_NAME = "trace_session"
_failures: OrderedDict[str, deque[float]] = OrderedDict()
_DUMMY_HASH = "scrypt$" + "00" * 16 + "$" + "00" * 64


def _derive(password: str, salt: bytes) -> bytes:
    # 32 MiB per verification; run in a worker thread rather than blocking the API loop.
    return hashlib.scrypt(password.encode(), salt=salt, n=32768, r=8, p=3, maxmem=64 * 1024 * 1024)


def password_mode() -> bool:
    return bool(settings.session_secret.get_secret_value())


def hash_password(password: str) -> str:
    if not 12 <= len(password) <= 1024:
        raise ValueError("Passwords must contain between 12 and 1024 characters")
    salt = secrets.token_bytes(16)
    digest = _derive(password, salt)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str | None) -> bool:
    try:
        algorithm, salt, expected = (encoded or _DUMMY_HASH).split("$")
        if algorithm != "scrypt" or len(salt) != 32 or len(expected) != 128:
            return False
        digest = _derive(password, bytes.fromhex(salt))
        return hmac.compare_digest(digest, bytes.fromhex(expected)) and encoded is not None
    except (ValueError, TypeError):
        return False


def _signature(payload: str) -> str:
    return hmac.new(
        settings.session_secret.get_secret_value().encode(), payload.encode(), hashlib.sha256
    ).hexdigest()


def encode_session(user: User) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps(
            {
                "uid": user.id,
                "exp": int(time.time()) + settings.session_hours * 3600,
                "version": user.session_version,
            },
            separators=(",", ":"),
        ).encode()
    ).decode()
    return f"{payload}.{_signature(payload)}"


def decode_session(request: Request) -> dict | None:
    token = request.cookies.get(COOKIE_NAME, "")
    if not token or len(token) > 1024 or not password_mode():
        return None
    try:
        payload, signature = token.split(".")
        if not hmac.compare_digest(signature, _signature(payload)):
            return None
        data = json.loads(base64.urlsafe_b64decode(payload))
        if not isinstance(data, dict):
            return None
        if any(type(data.get(field)) is not int for field in ("uid", "exp", "version")):
            return None
        if data["exp"] <= time.time():
            return None
        return data
    except (ValueError, TypeError, UnicodeError):
        return None


def set_session(response: Response, user: User, request: Request) -> None:
    response.set_cookie(
        COOKIE_NAME,
        encode_session(user),
        max_age=settings.session_hours * 3600,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="strict",
        path=request.scope.get("root_path") or "/",
    )


def clear_session(response: Response, request: Request) -> None:
    response.delete_cookie(
        COOKIE_NAME,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="strict",
        path=request.scope.get("root_path") or "/",
    )


def check_login_limit(email: str) -> str:
    """Bound password work and attempts per account, without retaining email addresses.

    This is a per-worker safeguard; the deployment proxy should also rate-limit login.
    """
    key = hashlib.sha256(email.casefold().encode()).hexdigest()
    now = time.monotonic()
    attempts = _failures.setdefault(key, deque())
    _failures.move_to_end(key)
    while attempts and attempts[0] < now - 300:
        attempts.popleft()
    if len(attempts) >= 10:
        raise HTTPException(
            429,
            "Too many login attempts; try again later",
            headers={"Retry-After": str(max(1, int(attempts[0] + 300 - now)))},
        )
    attempts.append(now)
    while len(_failures) > 2048:
        _failures.popitem(last=False)
    return key


def login_succeeded(key: str) -> None:
    _failures.pop(key, None)
