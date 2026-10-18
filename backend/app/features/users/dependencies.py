from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select

from app.common.exceptions import PermissionDeniedError, UnauthenticatedError
from app.core.api_auth import API_USER_EMAIL, BearerIdentity
from app.core.config import settings
from app.core.database import Session
from app.features.users.model import User
from app.features.users.security import decode_session, password_mode


# Bearer callers always use the reserved manager. The public demo browser selects its identity.
async def current_user(
    session: Session,
    authenticated: BearerIdentity,
    x_user_id: Annotated[int | None, Header()] = None,
    request: Request = None,
) -> User:
    """Resolve the API manager or the demo browser's chosen user."""
    if authenticated:
        user = await session.scalar(select(User).where(User.email == API_USER_EMAIL))
        if user is None or user.role != "manager":
            raise HTTPException(503, "API manager is unavailable; run database migrations")
        return user
    if password_mode():
        data = decode_session(request) if request is not None else None
        user = await session.get(User, data["uid"]) if data else None
        if user is None or user.password_hash is None or user.session_version != data["version"]:
            raise UnauthenticatedError("Sign in with your email and password")
        return user
    user = None if x_user_id is None else await session.get(User, x_user_id)
    if user is None:
        raise UnauthenticatedError(
            "Send X-User-Id with the id POST /login returns"
            if x_user_id is None
            else f"User {x_user_id} does not exist"
        )
    return user


CurrentUser = Annotated[User, Depends(current_user)]


async def manager_user(user: CurrentUser) -> User:
    """The app's user is the manager: only a manager runs, syncs, configures and resolves."""
    if user.role != "manager":
        raise PermissionDeniedError("Only a manager can do this")
    return user


Manager = Annotated[User, Depends(manager_user)]


async def optional_user(
    session: Session,
    authenticated: BearerIdentity,
    x_user_id: Annotated[int | None, Header()] = None,
    request: Request = None,
) -> User | None:
    """The caller when it says who it is: for the author of a step anyone may take."""
    if not password_mode() and not authenticated and x_user_id is None:
        return None
    return await current_user(session, authenticated, x_user_id, request)


OptionalUser = Annotated[User | None, Depends(optional_user)]


async def require_application_session(
    request: Request, session: Session, authenticated: BearerIdentity
) -> None:
    """Protect every business route, including reads that historically had no identity."""
    if not password_mode():
        return
    if request.method not in {"GET", "HEAD", "OPTIONS"} and not authenticated:
        origin = request.headers.get("origin")
        configured = urlsplit(settings.console_base_url)
        local = urlsplit(str(request.url))
        allowed = {f"{local.scheme}://{local.netloc}", f"{configured.scheme}://{configured.netloc}"}
        if (origin and origin.rstrip("/") not in allowed) or (
            request.headers.get("sec-fetch-site") == "cross-site"
        ):
            raise HTTPException(403, "Cross-origin browser requests are not allowed")
    path = request.scope["path"]
    root = request.scope.get("root_path", "")
    if root and path.startswith(root):
        path = path[len(root) :]
    if path in {"/login", "/logout", "/auth-config", "/health", "/ready", "/guide"}:
        return
    if path.startswith("/mail-ingestion/"):
        return  # MailIdentity enforces the separate, process-scoped credential.
    await current_user(session, authenticated, request=request)
