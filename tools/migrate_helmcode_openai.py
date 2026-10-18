"""Replace Helmcode in active configuration through the ordinary audited version services.

Run with the target database configured, after migration 0020. This is an explicit
administrative operation: it refuses pending process drafts and retains all history.
No documents are sent to a model; validation replays deterministic rules locally.
"""

import asyncio
import json
from copy import deepcopy

from app.core.database import session_factory
from app.features.processes.execution import ExecutionSettings, read, write
from app.features.processes.model import Process
from app.features.use_cases import service as agents
from app.features.use_cases.model import AgentConfig
from app.features.use_cases.schemas import AgentSettings
from app.features.versions import execution as replay
from app.features.versions import service
from app.features.versions.model import ProcessDraft
from app.features.versions.schemas import DraftIn
from sqlalchemy import select

MODEL = "openai-responses:gpt-6-luna"
READER = "compatible:gpt-6-luna"
ENDPOINT = "https://api.openai.com/v1"
AUTHOR = "Deployment administrator"
REASON = (
    "Replace Helmcode with OpenAI GPT-6 Luna at the owner's request; "
    "retain deterministic rules and historical evidence."
)


def agent_config(original: dict) -> dict:
    config = deepcopy(original)
    primary = config.get("model")
    if primary and primary.startswith("helmcode:"):
        config["model"] = MODEL
        config.setdefault("model_settings", {}).pop("temperature", None)
        config["model_settings"]["openai_reasoning_effort"] = "low"
    fallbacks = [
        MODEL if m.startswith("helmcode:") else m for m in config.get("fallback_models", [])
    ]
    config["fallback_models"] = list(
        dict.fromkeys(m for m in fallbacks if m != config.get("model"))
    )
    return config


def execution_config(original: dict) -> dict:
    config = deepcopy(original)
    config["agents"] = {role: agent_config(value) for role, value in config["agents"].items()}
    extraction = config["extraction"]
    for field in ("vision_model", "text_judge_model"):
        if (extraction.get(field) or "").startswith("helmcode:"):
            extraction[field] = READER
            config["compatible_endpoint"] = ENDPOINT
    # Two calls to the same Luna model are not independent verification.
    extraction["vision_verification_models"] = [
        model
        for model in extraction.get("vision_verification_models", [])
        if not model.startswith("helmcode:")
    ]
    return config


async def main():
    result = {"agents": [], "processes": []}
    async with session_factory() as session:
        if await session.scalar(select(ProcessDraft.process_id).limit(1)) is not None:
            raise RuntimeError(
                "Pending process drafts exist; preserve them before migrating providers"
            )
        configs = list(await session.scalars(select(AgentConfig).where(AgentConfig.active)))
        # Reject incompatible existing settings and failed historical validation before
        # any service commits a provider change. Deployment must still hold maintenance.
        for row in configs:
            AgentSettings.model_validate(agent_config(row.config))
        process_ids = list(await session.scalars(select(Process.id).order_by(Process.id)))
        for process_id in process_ids:
            version = await service.active(session, process_id, required=False)
            if version is None:
                continue
            original = read(version.snapshot).model_dump(mode="json")
            transformed = execution_config(original)
            if transformed == original:
                continue
            candidate = deepcopy(version.snapshot)
            write(candidate, ExecutionSettings.model_validate(transformed))
            report = await service.inspect(
                session, candidate, await replay.capture(session, process_id)
            )
            if not report.get("valid"):
                raise RuntimeError(f"Process {process_id} failed migration preflight: {report}")
        for row in configs:
            transformed = agent_config(row.config)
            if transformed == row.config:
                continue
            previous = row.id
            new = await agents.configure(
                session,
                row.use_case_id,
                row.role,
                AgentSettings.model_validate(transformed),
                AUTHOR,
                REASON,
            )
            result["agents"].append({"previous": previous, "current": new.id, "role": row.role})
        process_ids = list(await session.scalars(select(Process.id).order_by(Process.id)))
        for process_id in process_ids:
            version = await service.active(session, process_id, required=False)
            if version is None:
                continue
            original = read(version.snapshot).model_dump(mode="json")
            transformed = execution_config(original)
            if transformed == original:
                continue
            old_id, old_number = version.id, version.number
            draft = await service.edit(
                session,
                process_id,
                DraftIn(execution=ExecutionSettings.model_validate(transformed)),
                AUTHOR,
            )
            draft = await service.validate(session, process_id, AUTHOR)
            if not draft.validation or not draft.validation.get("valid"):
                raise RuntimeError(f"Process {process_id} failed validation: {draft.validation}")
            published = await service.publish(
                session, process_id, draft.revision, draft.validation["hash"], AUTHOR, REASON
            )
            result["processes"].append(
                {
                    "id": process_id,
                    "previous_version_id": old_id,
                    "previous_number": old_number,
                    "current_version_id": published.id,
                    "current_number": published.number,
                    "validation_valid": published.validation["valid"],
                }
            )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
