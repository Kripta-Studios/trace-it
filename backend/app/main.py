from contextlib import asynccontextmanager
from pathlib import Path

import logfire
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import text

from app.common.exceptions import TraceError
from app.core.api_auth import bearer_identity
from app.core.database import Session
from app.core.events import configure_observability, unhandled_error
from app.features.agents.router import router as agents_router
from app.features.alerts.router import router as alerts_router
from app.features.database_api.router import router as database_router
from app.features.decisions.router import router as decisions_router
from app.features.ingestion.process_router import router as ingestion_router
from app.features.ingestion.router import create_router as create_extraction_router
from app.features.ingestion.runtime import ingestion_lifespan
from app.features.knowledge.router import router as knowledge_router
from app.features.learning.router import router as learning_router
from app.features.mail_ingestion.router import router as mail_router
from app.features.processes.draft_router import router as draft_router
from app.features.processes.router import router as processes_router
from app.features.proposals.router import router as proposals_router
from app.features.rules import service as rules_service
from app.features.rules.router import router as rules_router
from app.features.sources.router import router as sources_router
from app.features.traces.router import router as traces_router
from app.features.use_cases.router import router as use_cases_router
from app.features.users.dependencies import current_user, require_application_session
from app.features.users.router import router as users_router
from app.features.users.security import password_mode
from app.features.versions.router import router as versions_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with ingestion_lifespan(app):
        await rules_service.resume_compilations()
        yield


configure_observability()
app = FastAPI(
    title="trace-it",
    version="0.1.0",
    lifespan=lifespan,
    dependencies=[Depends(bearer_identity), Depends(require_application_session)],
    description=(
        "Deterministic decision processes with rules compiled to code by agents.\n\n"
        "Use `Authorization: Bearer <API_TOKEN>` for every application endpoint and "
        "the `/db` administration endpoints. Bearer calls act as the dedicated API manager; "
        "`X-User-Id` is ignored. Private deployments authenticate browser users with "
        "passwords and secure session cookies (see `POST /login`). Development identity "
        "headers are supported only when password mode is disabled. `/db` requires Bearer. "
        "Mail-ingestion endpoints require their separate process-scoped mail service token. "
        "See the [API guide](guide) for examples, permissions and deployment details. Errors are "
        '`{"code", "message"}`, except request validation (422), which keeps FastAPI\'s '
        '`{"detail": [...]}`.'
    ),
)

logfire.instrument_fastapi(app)

# Password sessions are same-origin only; legacy development clients retain open CORS.
if not password_mode():
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
    )


@app.exception_handler(TraceError)
async def trace_error(_: Request, error: TraceError) -> JSONResponse:
    return JSONResponse(
        status_code=error.status_code, content={"code": error.code, "message": error.message}
    )


app.add_exception_handler(Exception, unhandled_error)


@app.get("/guide", include_in_schema=False, response_class=PlainTextResponse)
async def api_guide() -> str:
    return (Path(__file__).resolve().parents[2] / "docs" / "production-api.md").read_text()


@app.get("/health", tags=["system"], operation_id="health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready", tags=["system"], operation_id="ready")
async def ready(session: Session) -> dict[str, str]:
    """Readiness includes PostgreSQL; liveness alone must not approve a release."""
    await session.execute(text("SELECT 1"))
    return {"status": "ok"}


for router in (
    database_router,
    mail_router,
    versions_router,
    users_router,
    processes_router,
    draft_router,
    rules_router,
    decisions_router,
    agents_router,
    ingestion_router,
    sources_router,
    use_cases_router,
    traces_router,
    learning_router,
    knowledge_router,
    alerts_router,
    proposals_router,
):
    app.include_router(router)

app.include_router(create_extraction_router(), dependencies=[Depends(current_user)])

# The global administrator dependency deliberately defers mail routes to MailIdentity.
# Describe their actual required credential, rather than advertising the admin token there.
_application_openapi = app.openapi


def application_openapi():
    schema = _application_openapi()
    for path, operations in schema["paths"].items():
        if path.startswith("/mail-ingestion/"):
            for operation in operations.values():
                operation["security"] = [{"MailIngestionBearer": []}]
    return schema


app.openapi = application_openapi
