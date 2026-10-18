from datetime import datetime

from pydantic import BaseModel


class KnowledgeDocument(BaseModel):
    instance_id: int
    name: str
    status: str
    uploaded_at: datetime
    has_text: bool
    has_evidence: bool
    excerpt: str


class KnowledgePage(BaseModel):
    items: list[KnowledgeDocument]
    next_cursor: int | None
    query: str
