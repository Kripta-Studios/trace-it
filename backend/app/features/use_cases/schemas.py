from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

# compiler: writes a rule's code; tester: writes its tests and reviews disputes;
# assistant: suggests how to resolve an escalated case; normalizer: turns a norm in
# natural language into rule texts.
Role = Literal[
    "compiler", "tester", "assistant", "normalizer", "decision_reviewer", "learner", "discovery"
]
ROLES: tuple[Role, ...] = (
    "compiler",
    "tester",
    "assistant",
    "normalizer",
    "decision_reviewer",
    "learner",
    "discovery",
)


class Example(BaseModel):
    """A rule of this use case and code a person approved for it: shown to the compiler as
    a model of the use case's conventions, never for the rule being compiled."""

    text: str
    type: Literal["requirement", "prohibition"]
    code: str


class AgentSettings(BaseModel):
    """How one agent role works in a use case. Empty fields fall back to the platform
    defaults (`TRACE_<ROLE>_MODEL`, the limits in `agents/compiler.py`)."""

    model: str | None = Field(None, examples=["helmcode:deepseek-v4-flash"])
    # Tried in order when the model before fails at the provider (5xx, 429 after the SDK's
    # retries, timeout, connection) or its answer is cut by the output-token limit; a
    # rejected answer (ModelRetry) never switches (ADR 0019).
    fallback_models: list[str] = Field([], examples=[["helmcode:glm5.3-flash"]])
    # Per provider request; None uses model_settings.timeout or the platform's bounded default.
    timeout_seconds: float | None = Field(default=None, gt=0, le=3600)
    retries: int | None = Field(default=None, ge=0, le=10)
    request_limit: int | None = Field(default=None, ge=1, le=100)
    # Domain guidance appended to the role's platform prompt.
    instructions: str = ""
    # PydanticAI model settings, e.g. {"temperature": 0}.
    model_settings: dict[str, Any] = {}
    # compiler: max_attempts, auto_activate_max_change; tester: min_tests, max_reviews; any
    # role: http_retries, SDK repeats before the next model (platform default: 0).
    limits: dict[str, float] = {}
    examples: list[Example] = []
    # normalizer: the decision of a check whose failure the norm does not name (ADR 0017).
    # A decision type of the process, never the default. None: the prompt's own fallbacks.
    failed_check_decision: str | None = None

    @field_validator("limits")
    @classmethod
    def bounded_effort(cls, limits):
        for name, minimum, maximum in (
            ("max_attempts", 1, 20),
            ("max_reviews", 0, 20),
            ("min_tests", 1, 100),
            ("http_retries", 0, 5),
        ):
            value = limits.get(name)
            if value is not None and (not minimum <= value <= maximum or value != int(value)):
                raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")
        return limits


class UseCaseOut(BaseModel):
    id: int
    name: str
    description: str


class AgentConfigOut(BaseModel):
    id: int
    role: str
    version: int
    config: AgentSettings
    active: bool
    author: str
    note: str | None
    created_at: datetime


class UseCaseDetail(UseCaseOut):
    agents: list[AgentConfigOut]  # the active version of each configured role


class AgentConfigIn(BaseModel):
    config: AgentSettings
    note: str | None = None


class UseCaseDefinition(BaseModel):
    """A use case as data: `processes/<pack>/use-case.json`."""

    name: str
    description: str = ""
    agents: dict[Role, AgentSettings] = {}
