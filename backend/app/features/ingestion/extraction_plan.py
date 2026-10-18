"""Build the extraction contract from the current process definition and enforced rules."""

import hashlib
import json
from functools import lru_cache

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.features.agents.compiler import read_keys
from app.features.processes.model import Process, Symbol
from app.features.processes.schemas import SymbolExtraction
from app.features.rules.model import ENFORCED, Rule
from app.features.versions.model import ProcessVersion


class ExtractionField(BaseModel):
    name: str
    type: str
    description: str = ""
    required: bool = False
    labels: list[str] = Field(default_factory=list)
    source: str = "document"


class ExtractionPlan(BaseModel):
    process_id: int
    fields: list[ExtractionField] = Field(default_factory=list)
    rules: list[dict] = Field(default_factory=list)
    warnings: list[dict] = Field(default_factory=list)
    version: str = "schema-v2"

    @property
    def fingerprint(self) -> str:
        return _digest(self.model_dump(mode="json"))

    @property
    def field_fingerprint(self) -> str:
        """Schema identity for extraction caches that do not depend on rule code."""
        return _digest(
            {
                "version": self.version,
                "process_id": self.process_id,
                "fields": [f.model_dump() for f in self.fields],
            }
        )

    @property
    def rule_fingerprint(self) -> str:
        return _digest(
            {"version": self.version, "process_id": self.process_id, "rules": self.rules}
        )


class ExtractionPlanOut(BaseModel):
    process_id: int
    fields: list[ExtractionField]
    rules: list[dict]
    warnings: list[dict]
    version: str
    fingerprint: str


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _default_source(name: str) -> str:
    return {"file_id": "filename", "free_text": "text"}.get(name, "document")


@lru_cache(maxsize=128)
def _analyze_snapshot(content_hash: str, snapshot_json: str) -> str:
    """Cache immutable serialized results; the hash includes process, fields and rule code.

    The canonical JSON is also part of the key to make hash collisions harmless. The
    cache is process-local: after a restart the next request simply computes it again.
    """
    snapshot = json.loads(snapshot_json)
    fields = [ExtractionField.model_validate(field) for field in snapshot["fields"]]
    declared = {field.name for field in fields}
    rules = []
    warnings = []
    for row in snapshot["rules"]:
        rule_id, rule_hash, status, code = (row["id"], row["hash"], row["status"], row["code"])
        instance_keys: set[str] = set()
        source_keys: set[str] = set()
        if code:
            try:
                instance_keys, source_keys = read_keys(code)
            except SyntaxError:
                warnings.append({"code": "SCHEMA_INVALID_RULE_CODE", "rule_id": rule_id})
        rules.append(
            {
                "id": rule_id,
                "hash": rule_hash,
                "status": status,
                "code_digest": hashlib.sha256((code or "").encode("utf-8")).hexdigest(),
                "instance_keys": sorted(instance_keys),
                "source_keys": sorted(source_keys),
            }
        )
        for name in sorted(instance_keys - declared):
            warnings.append(
                {"code": "SCHEMA_UNDECLARED_SYMBOL", "rule_id": rule_id, "symbol": name}
            )
    return ExtractionPlan(
        process_id=snapshot["process_id"], fields=fields, rules=rules, warnings=warnings
    ).model_dump_json()


async def load_extraction_plan(session: AsyncSession, process_id: int) -> ExtractionPlan:
    """Use the published contract, or live tables before the first publication."""
    # Query scalar columns so an ORM Process already loaded in this session cannot
    # hide a newly published active_version_id.
    active_version_id = await session.scalar(
        select(Process.active_version_id).where(Process.id == process_id)
    )
    if active_version_id is not None:
        published = await session.scalar(
            select(ProcessVersion.snapshot).where(
                ProcessVersion.id == active_version_id,
                ProcessVersion.process_id == process_id,
            )
        )
        symbol_rows = [
            (
                row["name"],
                row["type"],
                row.get("description", ""),
                row.get("required", False),
                row.get("extraction"),
            )
            for row in sorted(published["process"]["symbols"], key=lambda row: row["name"])
        ]
        rule_rows = [
            (row["id"], row.get("hash"), row["status"], row.get("code"))
            for row in sorted(published["rules"], key=lambda row: row["id"])
            if row["status"] in ENFORCED
        ]
    else:
        symbol_rows = (
            await session.execute(
                select(
                    Symbol.name,
                    Symbol.type,
                    Symbol.description,
                    Symbol.required,
                    Symbol.extraction,
                )
                .where(Symbol.process_id == process_id)
                .order_by(Symbol.name)
            )
        ).all()
        rule_rows = (
            await session.execute(
                select(Rule.id, Rule.hash, Rule.status, Rule.code)
                .where(Rule.process_id == process_id, Rule.status.in_(ENFORCED))
                .order_by(Rule.id)
            )
        ).all()
    fields = []
    for name, type_, description, required, extraction_data in symbol_rows:
        metadata = SymbolExtraction.model_validate(extraction_data) if extraction_data else None
        fields.append(
            ExtractionField(
                name=name,
                type=type_,
                description=description or "",
                required=required,
                labels=metadata.labels if metadata else [],
                source=metadata.source if metadata else _default_source(name),
            )
        )

    snapshot_json = _canonical_json(
        {
            "process_id": process_id,
            "fields": [field.model_dump(mode="json") for field in fields],
            "rules": [
                {"id": id_, "hash": hash_, "status": status, "code": code}
                for id_, hash_, status, code in rule_rows
            ],
        }
    )
    # Parsing anew prevents a caller from mutating the object shared by future requests.
    content_hash = hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()
    return ExtractionPlan.model_validate_json(_analyze_snapshot(content_hash, snapshot_json))
