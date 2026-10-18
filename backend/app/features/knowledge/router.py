from typing import Annotated

from fastapi import APIRouter, Query

from app.core.database import Session
from app.features.knowledge import service
from app.features.knowledge.schemas import KnowledgePage
from app.features.users.dependencies import CurrentUser

router = APIRouter(tags=["knowledge"])


@router.get(
    "/processes/{process_id}/knowledge",
    operation_id="searchProcessDocuments",
    summary="Find persisted source documents by literal filename or extracted text",
    response_model=KnowledgePage,
)
async def search_documents(
    process_id: int,
    session: Session,
    user: CurrentUser,
    query: Annotated[str, Query(max_length=300)] = "",
    limit: Annotated[int, Query(ge=1, le=50)] = 25,
    cursor: Annotated[int | None, Query(ge=1)] = None,
):
    return await service.documents(session, process_id, query, limit, cursor)
