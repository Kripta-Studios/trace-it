"""Read-only candidate acceptance: never start workers or touch business data."""

import asyncio

import httpx
from app.core.config import settings
from app.features.users.security import password_mode
from app.main import app

assert (
    settings.require_password_auth
    and settings.session_cookie_secure
    and password_mode()
), "Private authentication must be mandatory"


async def check():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://localhost"
    ) as client:
        config = await client.get("/auth-config")
        assert config.status_code == 200 and config.json()["mode"] == "password"
        for path in ("/processes", "/users"):
            response = await client.get(path, headers={"X-User-Id": "1"})
            assert response.status_code == 401, (
                "Candidate allows anonymous business access"
            )


asyncio.run(check())
print("Private authentication deployment guard passed")
