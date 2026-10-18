"""Private deployment authentication: real database, synthetic users, no provider keys."""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr, ValidationError

from app.core.api_auth import API_USER_EMAIL
from app.core.config import Settings, settings
from app.core.database import session_factory
from app.features.users import security
from app.features.users.model import User
from app.features.users.password import provision
from app.main import app

PASSWORD = "synthetic-password-for-tests"


@pytest.fixture
async def secure(monkeypatch):
    monkeypatch.setattr(settings, "session_secret", SecretStr("test-session-secret-" * 3))
    security._failures.clear()
    async with session_factory() as session:
        users = [
            User(
                name=role,
                email=f"{role}-{uuid.uuid4().hex}@test",
                role=role,
                password_hash=security.hash_password(PASSWORD),
            )
            for role in ("manager", "operator")
        ]
        session.add_all(users)
        await session.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
        yield client, users
    security._failures.clear()


async def sign_in(client, user):
    response = await client.post("/login", json={"email": user.email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return response


async def test_secure_session_login_cookie_and_identity_forgery(secure):
    client, (manager, operator) = secure
    assert (await client.get("/auth-config")).json() == {"mode": "password", "session_hours": 12}
    for path in ("/processes", "/users", "/me", "/processes/1", "/health/planes"):
        assert (await client.get(path, headers={"X-User-Id": str(manager.id)})).status_code == 401
    assert (await client.get("/health")).status_code == 200
    response = await sign_in(client, operator)
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=strict" in cookie
    assert "password" not in response.json()
    assert "password_hash" not in response.json()
    assert (await client.get("/me", headers={"X-User-Id": str(manager.id)})).json()[
        "id"
    ] == operator.id
    assert (await client.get("/processes")).status_code == 200
    assert (
        await client.post(
            "/users",
            json={"name": "forged", "email": "forged@test"},
            headers={"X-User-Id": str(manager.id)},
        )
    ).status_code == 403


async def test_wrong_missing_unknown_and_disabled_passwords_have_same_failure(secure):
    client, (manager, _) = secure
    for body in (
        {"email": manager.email},
        {"email": manager.email, "password": "wrong"},
        {"email": "unknown@test", "password": PASSWORD},
        {"email": API_USER_EMAIL, "password": PASSWORD},
    ):
        response = await client.post("/login", json=body)
        assert response.status_code == 401
        assert response.json() == {"detail": "Invalid email or password"}
        assert security.COOKIE_NAME not in client.cookies


async def test_logout_revokes_copied_cookie(secure):
    client, (manager, _) = secure
    await sign_in(client, manager)
    token = client.cookies.get(security.COOKIE_NAME)
    assert (await client.post("/logout")).status_code == 204
    assert security.COOKIE_NAME not in client.cookies
    assert (await client.get("/me")).status_code == 401
    client.cookies.set(security.COOKIE_NAME, token)
    assert (await client.get("/processes")).status_code == 401
    client.cookies.clear()
    await sign_in(client, manager)
    assert (await client.get("/me")).status_code == 200


async def test_tampered_and_expired_cookie_fail_closed(secure, monkeypatch):
    client, (manager, _) = secure
    await sign_in(client, manager)
    token = client.cookies.get(security.COOKIE_NAME)
    client.cookies.clear()
    client.cookies.set(security.COOKIE_NAME, "x" + token)
    assert (await client.get("/processes")).status_code == 401
    client.cookies.set(security.COOKIE_NAME, token)
    # Use the signed expiry itself so this test does not depend on the wall clock.
    import base64
    import json

    expiry = json.loads(base64.urlsafe_b64decode(token.split(".")[0]))["exp"]
    monkeypatch.setattr(security.time, "time", lambda: expiry + 1)
    assert (await client.get("/me")).status_code == 401


async def test_password_change_revokes_session(secure):
    client, (manager, _) = secure
    await sign_in(client, manager)
    await provision(manager.email, "another-synthetic-password", None, None)
    assert (await client.get("/me")).status_code == 401
    response = await client.post("/login", json={"email": manager.email, "password": PASSWORD})
    assert response.status_code == 401


async def test_bearer_keeps_dedicated_identity_and_invalid_token_cannot_use_cookie(
    secure, monkeypatch
):
    client, (manager, _) = secure
    monkeypatch.setattr(settings, "api_token", SecretStr("synthetic-api-token"))
    response = await client.get(
        "/me", headers={"Authorization": "Bearer synthetic-api-token", "X-User-Id": str(manager.id)}
    )
    assert response.status_code == 200
    assert response.json()["email"] == API_USER_EMAIL
    await sign_in(client, manager)
    assert (await client.get("/me", headers={"Authorization": "Bearer invalid"})).status_code == 401


async def test_manager_creates_password_account_and_secrets_never_return(secure):
    client, (manager, _) = secure
    await sign_in(client, manager)
    body = {"name": "New colleague", "email": f"new-{uuid.uuid4().hex}@test", "role": "operator"}
    assert (await client.post("/users", json=body)).status_code == 422
    response = await client.post("/users", json={**body, "password": PASSWORD})
    assert response.status_code == 201, response.text
    assert set(response.json()) == {"id", "name", "email", "role"}
    assert (await client.post("/users", json={**body, "password": PASSWORD})).status_code == 409
    await client.post("/logout")
    assert (
        await client.post("/login", json={"email": body["email"], "password": PASSWORD})
    ).status_code == 200


async def test_login_throttle_and_cross_origin_rejection(secure):
    client, (manager, _) = secure
    for origin in ("https://attacker.invalid", "https://other.hopto.org", "null"):
        response = await client.post(
            "/login",
            headers={"Origin": origin},
            json={"email": manager.email, "password": PASSWORD},
        )
        assert response.status_code == 403
    await sign_in(client, manager)
    response = await client.post("/logout", headers={"Origin": "https://attacker.invalid"})
    assert response.status_code == 403
    assert (await client.get("/me")).status_code == 200
    for _ in range(10):
        assert (
            await client.post("/login", json={"email": manager.email, "password": "bad"})
        ).status_code == 401
    response = await client.post("/login", json={"email": manager.email, "password": PASSWORD})
    assert response.status_code == 429
    assert int(response.headers["retry-after"]) > 0


async def test_cookie_path_follows_proxy_mount(secure):
    _, (manager, _) = secure
    prefix = "/nexia/trace-it/api"
    async with AsyncClient(
        transport=ASGITransport(app=app, root_path=prefix), base_url="https://test" + prefix
    ) as client:
        response = await sign_in(client, manager)
        assert f"Path={prefix}" in response.headers["set-cookie"]
        assert (await client.get("/me")).status_code == 200
        response = await client.post("/logout")
        assert f"Path={prefix}" in response.headers["set-cookie"]


def test_short_secret_is_rejected_and_password_hash_is_salted():
    with pytest.raises(ValidationError):
        Settings(session_secret="too-short")
    with pytest.raises(ValidationError, match="Password authentication is required"):
        Settings(require_password_auth=True, session_secret="")
    assert Settings(require_password_auth=True, session_secret="x" * 32).require_password_auth
    first, second = security.hash_password(PASSWORD), security.hash_password(PASSWORD)
    assert first != second
    assert security.verify_password(PASSWORD, first)
    assert not security.verify_password("wrong", first)
    assert not security.verify_password(PASSWORD, None)
    assert not security.verify_password(PASSWORD, "malformed")
