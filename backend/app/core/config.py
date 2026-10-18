from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, from the environment or `.env` (prefix `TRACE_`)."""

    model_config = SettingsConfigDict(env_prefix="TRACE_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://trace:trace@localhost:5432/trace"
    # Empty disables Bearer access. Never put the production token in source control.
    api_token: SecretStr = SecretStr("")
    # Set a high-entropy secret to require password sessions for browser access.
    # An empty value retains the explicitly opt-in development identity selector.
    session_secret: SecretStr = SecretStr("")
    session_hours: int = Field(default=12, ge=1, le=168)
    session_cookie_secure: bool = True
    require_password_auth: bool = False

    @field_validator("session_secret")
    @classmethod
    def validate_session_secret(cls, value: SecretStr) -> SecretStr:
        if value.get_secret_value() and len(value.get_secret_value()) < 32:
            raise ValueError("session_secret must contain at least 32 characters")
        return value

    @model_validator(mode="after")
    def require_production_auth(self) -> "Settings":
        if self.require_password_auth and not self.session_secret.get_secret_value():
            raise ValueError("Password authentication is required; set TRACE_SESSION_SECRET")
        return self

    # Process packs, where a source's connector configuration lives (`<pack>/sources.json`).
    # In Docker this resolves to the /processes mount.
    processes_dir: Path = Path(__file__).resolve().parents[3] / "processes"
    # One model per agent role, in PydanticAI's `provider:model` form (ADR 0006): any
    # provider PydanticAI supports. The tester writes the tests the compiler's code must
    # pass (ADR 0004). Defaults: Vercel AI Gateway's GLM model, with `fallback_models`
    # tried in order when a role has no model of its own in the use case (ADR 0019).
    compiler_model: str = "vercel:zai/glm-5.3"
    tester_model: str = "vercel:zai/glm-5.3"
    assistant_model: str = "vercel:zai/glm-5.3"
    decision_reviewer_model: str = "vercel:zai/glm-5.3"
    learner_model: str = "vercel:zai/glm-5.3"
    normalizer_model: str = "vercel:zai/glm-5.3"
    discovery_model: str = "vercel:zai/glm-5.3"
    fallback_models: list[str] = ["helmcode:deepseek-v4-flash", "helmcode:qwen3.6"]
    # Bound provider waits for roles without their own settings. A provider failure moves
    # to the configured fallback instead of silently repeating a ten-minute SDK timeout.
    agent_timeout_seconds: float = Field(default=300, gt=0, le=3600)
    agent_http_retries: int = Field(default=0, ge=0, le=8)
    # OpenAI-compatible gateways. Select Helmcode with `helmcode:<model>` and Vercel AI
    # Gateway with `vercel:<provider/model>`. Credentials stay in their environment variables.
    # HELMCODE_URL (the ingestion's older name) is read when this one is not set.
    helmcode_base_url: str = Field(
        "https://api.helmcode.com/v1",
        validation_alias=AliasChoices("TRACE_HELMCODE_BASE_URL", "HELMCODE_URL"),
    )
    ai_gateway_base_url: str = "https://ai-gateway.vercel.sh/v1"
    # The console the exported `trace_url` links to, without a trailing slash.
    console_base_url: str = "https://gex-dashboard.hopto.org/nexia/trace-it"
    # The deployment's trusted local OpenAI-compatible server. Key: LOCAL_LLM_API_KEY.
    local_base_url: str = "http://localhost:11434/v1"
    # Optional per-preset model/effort overrides. No credentials in this JSON.
    execution_presets: dict[str, dict] = {}
    # A compiled rule activates by itself when it would change at most this share of the
    # decisions already taken and contradicts no decision a person took (ADR 0004).
    auto_activate_max_change: float = 0.05
    # Rules one background job compiles at once (Helmcode allows 5 concurrent requests
    # per model).
    compile_concurrency: int = 5
    # Independent rule sandboxes that may run at once. Four was the best measured
    # default on the reference machine; higher values mostly add JSON and memory pressure.
    decision_workers: int = 4
    # `GET /health/planes`: over the last `health_window_minutes`, a plane is `down` from
    # this error rate, `degraded` from the lower one or when its p95 span duration passes
    # its limit (`TRACE_HEALTH_P95_MS='{"agents": 90000}'`). Below `health_min_spans` spans
    # in the window a plane is `ok` ("not enough data"): one failed span out of one is noise.
    health_window_minutes: int = 15
    health_min_spans: int = 5
    health_degraded_error_rate: float = 0.05
    health_down_error_rate: float = 0.5
    health_p95_ms: dict[str, int] = {"ingestion": 10_000, "agents": 60_000, "execution": 5_000}


settings = Settings()
