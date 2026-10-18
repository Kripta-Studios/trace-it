from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from app.common.exceptions import ConflictError, NotFoundError
from app.core.config import settings
from app.core.database import Session
from app.features.users.dependencies import CurrentUser, Manager
from app.features.users.model import User
from app.features.users.schemas import CreateUserIn, LoginIn, UserOut
from app.features.users.security import (
    check_login_limit,
    clear_session,
    decode_session,
    hash_password,
    login_succeeded,
    password_mode,
    set_session,
    verify_password,
)

router = APIRouter(tags=["users"])


def _out(user: User) -> UserOut:
    return UserOut(id=user.id, name=user.name, email=user.email, role=user.role)


@router.get("/users", operation_id="listUsers", summary="All users")
async def list_users(session: Session) -> list[UserOut]:
    return [_out(u) for u in await session.scalars(select(User).order_by(User.id))]


@router.post(
    "/users",
    operation_id="createUser",
    status_code=status.HTTP_201_CREATED,
    summary="Create a user",
    responses={409: {"description": "Email already in use"}},
)
async def create_user(body: CreateUserIn, session: Session, _: Manager) -> UserOut:
    if password_mode() and body.password is None:
        raise HTTPException(422, "A password of at least 12 characters is required")
    if await session.scalar(select(User).where(User.email == body.email)):
        raise ConflictError(f"A user with email {body.email} already exists")
    user = User(**body.model_dump(exclude={"password"}))
    if body.password is not None:
        user.password_hash = await run_in_threadpool(
            hash_password, body.password.get_secret_value()
        )
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise ConflictError("A user with this email already exists") from None
    return _out(user)


@router.post(
    "/login",
    operation_id="login",
    summary="Sign in; password deployments issue a secure browser session cookie",
)
async def login(body: LoginIn, session: Session, response: Response, request: Request) -> UserOut:
    key = check_login_limit(body.email) if password_mode() else None
    user = await session.scalar(select(User).where(User.email == body.email))
    if password_mode():
        valid = await run_in_threadpool(
            verify_password,
            body.password.get_secret_value() if body.password else "",
            user.password_hash if user else None,
        )
        if not valid:
            raise HTTPException(401, "Invalid email or password")
        login_succeeded(key)
        set_session(response, user, request)
        response.headers["Cache-Control"] = "no-store"
        return _out(user)
    if user is None:
        raise NotFoundError(f"No user with email {body.email}")
    return _out(user)


@router.get("/auth-config", operation_id="getAuthConfig", summary="Browser authentication mode")
async def auth_config() -> dict:
    return {
        "mode": "password" if password_mode() else "identity",
        "session_hours": settings.session_hours,
    }


@router.post("/logout", operation_id="logout", status_code=204, summary="End browser sessions")
async def logout(request: Request, response: Response, session: Session) -> None:
    data = decode_session(request)
    if data:
        # Logging out revokes all cookies for this user, including copied cookies.
        await session.execute(
            update(User)
            .where(User.id == data["uid"], User.session_version == data["version"])
            .values(session_version=User.session_version + 1)
        )
        await session.commit()
    clear_session(response, request)
    response.headers["Cache-Control"] = "no-store"


@router.get("/me", operation_id="me", summary="The current user")
async def me(user: CurrentUser) -> UserOut:
    return _out(user)
