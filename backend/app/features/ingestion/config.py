import os
from dataclasses import dataclass, field
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]


def _providers(name: str, default: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            part.strip().lower() for part in os.getenv(name, default).split(",") if part.strip()
        )
    )


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(
        default_factory=lambda: Path(os.getenv("TRACEPAY_DATA_DIR", str(REPOSITORY_ROOT / ".data")))
    )
    model_dir: Path = field(
        default_factory=lambda: Path(
            os.getenv("TRACEPAY_MODEL_DIR", str(REPOSITORY_ROOT / ".models"))
        )
    )
    verification_model_dir: Path | None = None
    max_file_bytes: int = 25 * 1024 * 1024
    max_pages: int = 40
    max_image_pixels: int = 18_000_000
    max_excel_cells: int = 500_000
    max_excel_rows: int = 25_000
    max_excel_cols: int = 100
    workers: int = field(default_factory=lambda: int(os.getenv("TRACEPAY_WORKERS", "2")))
    extraction_timeout: float = field(
        default_factory=lambda: float(os.getenv("TRACEPAY_EXTRACTION_TIMEOUT_S", "120"))
    )
    ocr_threads: int = field(default_factory=lambda: int(os.getenv("TRACEPAY_OCR_THREADS", "4")))
    ocr_use_cuda: bool = field(default_factory=lambda: os.getenv("TRACEPAY_OCR_CUDA", "0") == "1")
    ocr_dpi: int = 240
    ocr_profile: str = field(
        default_factory=lambda: os.getenv("TRACEPAY_OCR_PROFILE", "experimental")
    )
    ocr_min_confidence: float = 0.90
    ocr_mode: str = field(default_factory=lambda: os.getenv("TRACEPAY_OCR_MODE", "api").lower())
    vlm_url: str | None = field(default_factory=lambda: os.getenv("TRACEPAY_VLM_URL"))
    vlm_model: str | None = field(default_factory=lambda: os.getenv("TRACEPAY_VLM_MODEL"))
    vlm_api_key: str | None = field(
        default_factory=lambda: os.getenv("TRACEPAY_VLM_API_KEY"), repr=False
    )
    vlm_timeout: float = field(
        default_factory=lambda: float(os.getenv("TRACEPAY_PROVIDER_TIMEOUT_S", "60"))
    )
    vision_max_tokens: int = 4096
    judge_url: str | None = None
    judge_api_key: str | None = field(default=None, repr=False)
    judge_timeout: float = 60.0
    judge_max_tokens: int = 4096
    helmcode_api_key: str | None = field(
        default_factory=lambda: os.getenv("HELMCODE_API_KEY"), repr=False
    )
    helmcode_url: str = field(
        default_factory=lambda: (
            os.getenv("TRACE_HELMCODE_BASE_URL")
            or os.getenv("HELMCODE_URL", "https://api.helmcode.com/v1")
        )
    )
    helmcode_vision_models: tuple[str, ...] = field(
        default_factory=lambda: _providers("TRACEPAY_HELMCODE_VISION_MODELS", "qwen3.6,gemma4")
    )
    helmcode_text_model: str = field(
        default_factory=lambda: os.getenv("TRACEPAY_HELMCODE_TEXT_MODEL", "qwen3.6")
    )
    vision_providers: tuple[str, ...] = field(
        default_factory=lambda: _providers("TRACEPAY_VISION_PROVIDERS", "helmcode")
    )
    # Explicit versioned readers override the deployment's default chain.
    visual_readers: tuple[tuple[str, str], ...] | None = None
    text_url: str | None = field(default_factory=lambda: os.getenv("TRACEPAY_TEXT_URL"))
    text_model: str | None = field(default_factory=lambda: os.getenv("TRACEPAY_TEXT_MODEL"))
    text_api_key: str | None = field(
        default_factory=lambda: os.getenv("TRACEPAY_TEXT_API_KEY"), repr=False
    )
    text_providers: tuple[str, ...] = field(
        default_factory=lambda: _providers("TRACEPAY_TEXT_PROVIDERS", "jev,helmcode")
    )
    gemini_model: str = field(
        default_factory=lambda: os.getenv("TRACEPAY_GEMINI_MODEL", "gemini-3.1-flash-lite")
    )
    gemini_api_key: str | None = field(
        default_factory=lambda: os.getenv("GEMINI_API_KEY"), repr=False
    )
    jev_model: str = field(default_factory=lambda: os.getenv("TRACEPAY_JEV_MODEL", "jev-1.13.0"))
    jev_api_key: str | None = field(
        default_factory=lambda: os.getenv("TYPESAFE_API_KEY"), repr=False
    )
    # Demo switch: ignore extraction caches, OCR caches and the provider journal.
    ocr_force_recompute: bool = field(
        default_factory=lambda: os.getenv("TRACEPAY_OCR_FORCE_RECOMPUTE", "0") == "1"
    )
    max_batch_files: int = 100
    max_queued_files: int = 1000

    def __post_init__(self):
        if self.ocr_profile not in {"verified", "experimental"}:
            raise ValueError("TRACEPAY_OCR_PROFILE must be verified or experimental")
        if not 1 <= self.workers <= 8 or not 1 <= self.ocr_threads <= 16:
            raise ValueError("Use 1-8 workers and 1-16 OCR threads to bound memory and CPU use")
        if self.ocr_mode not in {"local", "api", "hybrid"}:
            raise ValueError("TRACEPAY_OCR_MODE must be local, api, or hybrid")
        if self.vlm_timeout <= 0 or self.extraction_timeout <= 0:
            raise ValueError("TRACEPAY_PROVIDER_TIMEOUT_S must be positive")
        if set(self.vision_providers) - {"compatible", "gemini", "helmcode"}:
            raise ValueError("Unknown TRACEPAY_VISION_PROVIDERS entry")
        if set(self.text_providers) - {"jev", "helmcode", "compatible"}:
            raise ValueError("Unknown TRACEPAY_TEXT_PROVIDERS entry")
        if set(self.helmcode_vision_models) - {"qwen3.6", "gemma4"}:
            raise ValueError("Helmcode vision supports qwen3.6 and gemma4")

    def summary(self) -> dict:
        """Public provider configuration without credentials."""
        return {
            "profile": self.ocr_profile,
            "mode": self.ocr_mode,
            "independent_visual_readers": len(
                {model.rsplit("/", 1)[-1].lower() for _, model in self.visual_chain()}
            ),
            "vision": [
                {"provider": provider, "model": model} for provider, model in self.visual_chain()
            ],
            "text": [
                {
                    "provider": provider,
                    "model": self.text_model
                    if provider == "compatible"
                    else (self.jev_model if provider == "jev" else self.helmcode_text_model),
                }
                for provider in self.text_providers
                if (provider == "jev" and self.jev_api_key)
                or (provider == "helmcode" and self.helmcode_api_key)
                or (provider == "compatible" and self.text_url and self.text_model)
            ],
        }

    def visual_chain(self) -> list[tuple[str, str]]:
        if self.visual_readers is not None:
            return [
                (provider, model)
                for provider, model in self.visual_readers
                if (provider == "compatible" and self.vlm_url)
                or (provider == "gemini" and self.gemini_api_key)
                or (provider == "helmcode" and self.helmcode_api_key)
            ]
        chain = []
        for provider in self.vision_providers:
            if provider == "compatible" and self.vlm_url and self.vlm_model:
                chain.append((provider, self.vlm_model))
            elif provider == "gemini" and self.gemini_api_key and self.gemini_model:
                chain.append((provider, self.gemini_model))
            elif provider == "helmcode" and self.helmcode_api_key:
                chain.extend((provider, model) for model in self.helmcode_vision_models)
        return chain
