"""Extract schema-selected fields from OCR lines without executing document metadata."""

import json
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app.common import prompts
from app.common.extraction import Candidate, Evidence, TextLine
from app.common.normalization import clean_text, fold, invoice_date
from app.features.ingestion.ocr.http import ProviderHTTP

from .extraction_plan import ExtractionField
from .ocr.errors import ProviderUnavailable, note_provider_failure, provider_on_cooldown
from .ocr.journal import record_response, recorded_call, retry_after
from .pdf.layout import table_cells
from .schemas import FieldReading

SUPPORTED_TYPES = {"text", "string", "number", "integer", "date", "boolean"}
MAX_LINES = 200
MAX_TRANSCRIPT = 20_000
MAX_FIELDS = 50
PROMPTS = Path(__file__).parent / "prompts"
PROMPT = prompts.read(PROMPTS, "select-fields")


def _type(field: ExtractionField) -> str:
    return str(field.type).lower()


def _labels(field: ExtractionField) -> list[str]:
    labels = [*field.labels, field.name, field.name.replace("_", " ").replace("-", " ")]
    description = clean_text(field.description or "")
    if description and len(description) <= 40 and len(description.split()) <= 5:
        labels.append(description)
    return list(dict.fromkeys(fold(label) for label in labels if clean_text(label)))


def _fold_line(value: str) -> tuple[str, list[int]]:
    """Fold for matching while retaining offsets into the original OCR line."""
    folded = []
    offsets = []
    for index, char in enumerate(value):
        expanded = unicodedata.normalize("NFKC", char)
        expanded = "".join(
            part
            for part in unicodedata.normalize("NFD", expanded)
            if not unicodedata.combining(part)
        ).upper()
        folded.append(expanded)
        offsets.extend([index] * len(expanded))
    return "".join(folded), offsets


def normalize_schema_value(raw: str, kind: str) -> str:
    """Validate a literal schema value using the same types as OCR readings."""
    value = clean_text(raw).strip(" \t:;#=")
    if not value or "[ILLEGIBLE]" in value.upper() or "?" in value:
        raise ValueError("Missing or unreadable value")
    if kind in {"text", "string"}:
        return value
    if kind == "date":
        return invoice_date(value)
    if kind == "boolean":
        answer = fold(value)
        if answer in {"YES", "TRUE", "SI", "1"}:
            return "true"
        if answer in {"NO", "FALSE", "0"}:
            return "false"
        raise ValueError("Invalid boolean")
    if kind in {"number", "integer"}:
        # A literal may carry one currency annotation, without changing its amount.
        # Only trim a known token at an edge: embedded units, prose, and multiple
        # currencies must still fail the numeric grammar below. Never round or convert.
        value = re.sub(
            r"^(?:EUR|USD|GBP|[€$£])\s*|\s*(?:EUR|USD|GBP|[€$£])$",
            "",
            value,
            count=1,
            flags=re.IGNORECASE,
        )
        compact = re.sub(r"[ \u00a0]", "", value)
        if not re.fullmatch(r"[+-]?\d[\d.,]*", compact):
            raise ValueError("Invalid number")
        sign = "-" if compact.startswith("-") else ""
        unsigned = compact.lstrip("+-")
        if "," in unsigned and "." in unsigned:
            decimal_sep = "," if unsigned.rfind(",") > unsigned.rfind(".") else "."
            group_sep = "." if decimal_sep == "," else ","
            left, right = unsigned.rsplit(decimal_sep, 1)
            if not re.fullmatch(rf"\d{{1,3}}(?:{re.escape(group_sep)}\d{{3}})+", left):
                raise ValueError("Invalid grouping")
            unsigned = left.replace(group_sep, "") + "." + right
        elif "," in unsigned or "." in unsigned:
            sep = "," if "," in unsigned else "."
            parts = unsigned.split(sep)
            if len(parts) == 2:
                # A lone three-digit suffix can be thousands or decimals: abstain.
                if len(parts[1]) == 3:
                    raise ValueError("Ambiguous separator")
                unsigned = parts[0] + "." + parts[1]
            elif len(parts) > 2 and all(len(part) == 3 for part in parts[1:]):
                unsigned = "".join(parts)
            else:
                raise ValueError("Invalid grouping")
        try:
            number = Decimal(sign + unsigned)
        except InvalidOperation as exc:
            raise ValueError("Invalid number") from exc
        if not number.is_finite() or (kind == "integer" and number != number.to_integral_value()):
            raise ValueError("Invalid number")
        return format(number, "f")
    raise ValueError("Unsupported field type")


def _candidate(raw: str, line: TextLine, kind: str) -> Candidate:
    try:
        value, error = normalize_schema_value(raw, kind), None
    except ValueError as exc:
        value, error = None, str(exc)
    return Candidate(
        value=value,
        raw=raw,
        error=error,
        evidence=Evidence(
            locator=line.id,
            text=line.raw,
            page=line.page,
            bbox=line.bbox,
            method=line.method,
            confidence=line.confidence,
            preprocessing=line.preprocessing,
        ),
    )


def _reading(candidates: list[Candidate], min_confidence: float, selected_by: str) -> FieldReading:
    valid = [c for c in candidates if c.value is not None and c.error is None]
    values = {c.value for c in valid}
    conflict = len(values) > 1 or (valid and len(valid) != len(candidates))
    reliable = [
        c
        for c in valid
        if c.evidence.method == "native"
        or (
            c.evidence.method == "ocr"
            and c.evidence.confidence is not None
            and c.evidence.confidence >= min_confidence
        )
    ]

    def visual_identity(candidate):
        locator = candidate.evidence.locator
        match = re.search(r"(?:^|:)visual:([^:]+):([^:]+):", locator)
        return f"visual:{match[1]}:{match[2]}" if match else None

    visual = {
        identity.rsplit(":", 1)[-1].rsplit("/", 1)[-1].lower()
        for candidate in valid
        if (identity := visual_identity(candidate))
    }
    selected = (
        (reliable[0] if reliable else valid[0])
        if len(values) == 1 and not conflict and (reliable or len(visual) >= 2)
        else None
    )
    proposal = valid[0] if len(values) == 1 and not conflict and valid else None
    if conflict:
        verification = "ambiguous"
    elif selected:
        verification = (
            "verified"
            if selected.evidence.method == "native" and selected_by != "schema_model"
            else "extracted"
        )
    elif candidates and not valid:
        verification = "invalid"
    elif proposal:
        verification = "unverified"
    else:
        verification = "missing"
    return FieldReading(
        value=selected.value if selected else None,
        proposed_value=proposal.value if proposal else None,
        proposed_by=selected_by if proposal else None,
        verification=verification,
        text=selected.raw if selected else (candidates[0].raw if candidates else None),
        selected_by=selected_by if selected else None,
        agreeing_readers=sorted(
            {
                (visual_identity(c) or c.evidence.method)
                for c in valid
                if selected and c.value == selected.value
            }
        ),
        confidence=selected.evidence.confidence if selected else None,
        candidates=candidates,
    )


@dataclass(frozen=True)
class _Match:
    start: int
    end: int
    field: str


def read_schema_fields(
    lines: list[TextLine], fields: list[ExtractionField], min_confidence: float
) -> tuple[dict[str, FieldReading], list[dict]]:
    """Read explicit labels from text; ambiguous evidence remains visible as candidates."""
    warnings: list[dict] = []
    candidates: dict[str, list[Candidate]] = {field.name: [] for field in fields}
    collided_fields: set[str] = set()
    kinds = {field.name: _type(field) for field in fields}
    labels = [(field.name, label) for field in fields for label in _labels(field)]
    for field in fields:
        if kinds[field.name] not in SUPPORTED_TYPES:
            warnings.append({"code": "SCHEMA_UNSUPPORTED_TYPE", "field": field.name})
    for line in lines:
        folded, offsets = _fold_line(line.text)
        matches: list[_Match] = []
        for name, label in labels:
            if kinds[name] not in SUPPORTED_TYPES:
                continue
            pattern = rf"(?<![\w]){re.escape(label)}(?![\w])\s*[:#=]\s*"
            for match in re.finditer(pattern, folded):
                matches.append(_Match(offsets[match.start()], offsets[match.end() - 1] + 1, name))
        # Longest label wins when aliases overlap. Other labels delimit the value.
        chosen: list[_Match] = []
        for match in sorted(matches, key=lambda m: (m.start, -(m.end - m.start))):
            if not any(match.start < other.end and other.start < match.end for other in chosen):
                chosen.append(match)
        chosen.sort(key=lambda m: m.start)
        for index, match in enumerate(chosen):
            end = chosen[index + 1].start if index + 1 < len(chosen) else len(line.text)
            raw = line.text[match.end : end].strip(" \t:;#=")
            owners = sorted(
                {
                    item.field
                    for item in matches
                    if item.start == match.start and item.end == match.end
                }
            )
            if len(owners) > 1:
                collided_fields.update(owners)
                warnings.append(
                    {"code": "SCHEMA_LABEL_COLLISION", "fields": owners, "locator": line.id}
                )
                for name in owners:
                    candidates[name].append(
                        _candidate(raw, line, kinds[name]).model_copy(
                            update={"value": None, "error": "Ambiguous label shared by fields"}
                        )
                    )
                continue
            if raw:
                candidates[match.field].append(_candidate(raw, line, kinds[match.field]))
    # A two-column key/value row can be read without inventing a combined quote.
    # More complex tables retain cell context for the grounded semantic reader below.
    for cells in table_cells(lines).values():
        positions = [line.table for group in cells.values() for line in group]
        if positions[0].columns != 2:
            continue
        for row in range(positions[0].rows):
            label_lines, value_lines = cells.get((row, 0), []), cells.get((row, 1), [])
            if len(label_lines) != 1 or len(value_lines) != 1:
                continue
            # Without an explicit delimiter this could be a header row (Region | 2025),
            # not a label/value form. Let the semantic reader handle that distinction.
            if not label_lines[0].text.rstrip().endswith((":", "#", "=")):
                continue
            label = fold(label_lines[0].text).strip(" :#=")
            owners = {name for name, alias in labels if alias == label}
            if len(owners) != 1:
                continue
            name = next(iter(owners))
            if kinds[name] not in SUPPORTED_TYPES:
                continue
            line = value_lines[0]
            if not any(
                c.evidence.locator == line.id and c.raw == line.text for c in candidates[name]
            ):
                candidates[name].append(_candidate(line.text, line, kinds[name]))
    readings = {
        name: _reading(items, min_confidence, "schema_label") for name, items in candidates.items()
    }
    for name in collided_fields:
        readings[name] = readings[name].model_copy(
            update={
                "value": None,
                "proposed_value": None,
                "proposed_by": None,
                "verification": "ambiguous",
                "selected_by": None,
                "agreeing_readers": [],
                "confidence": None,
            }
        )
    return readings, warnings


class SchemaFieldReader:
    def __init__(self, settings, *, http=None):
        self.settings = settings
        self.http = http or ProviderHTTP()

    @property
    def configured(self) -> bool:
        return bool(self._chain())

    def _chain(self):
        """Use visual provider precedence but the Helmcode text model for mapping."""
        return list(
            dict.fromkeys(
                (
                    provider,
                    self.settings.helmcode_text_model
                    if provider == "helmcode" and "helmcode" in self.settings.text_providers
                    else model,
                )
                for provider, model in self.settings.visual_chain()
            )
        )

    def signature(self) -> dict:
        """Effective, credential-free schema mapping configuration for result caches."""
        return {
            "chain": [
                {
                    "provider": provider,
                    "model": model,
                    "endpoint": self._endpoint(provider, model),
                    "generation": (
                        {
                            "temperature": 0,
                            "maxOutputTokens": self._max_tokens(),
                            "responseMimeType": "application/json",
                        }
                        if provider == "gemini"
                        else {
                            "temperature": 0,
                            "max_tokens": self._max_tokens(),
                            **(
                                {
                                    "reasoning_effort": "none",
                                    "response_format": {"type": "json_object"},
                                }
                                if provider == "helmcode"
                                else {}
                            ),
                        }
                    ),
                }
                for provider, model in self._chain()
            ],
            "prompt": PROMPT,
            "timeout_seconds": self.settings.vlm_timeout,
            "max_tokens": self._max_tokens(),
        }

    def _max_tokens(self) -> int:
        return min(1500, self.settings.vision_max_tokens)

    def _endpoint(self, provider: str, model: str) -> str:
        if provider == "gemini":
            return (
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            )
        base = self.settings.vlm_url if provider == "compatible" else self.settings.helmcode_url
        return base.rstrip("/") + "/chat/completions"

    def read(
        self, lines: list[TextLine], fields: list[ExtractionField]
    ) -> tuple[dict[str, FieldReading], list[dict]]:
        readings, warnings = read_schema_fields(lines, fields, self.settings.ocr_min_confidence)
        unresolved = [
            field
            for field in fields
            if readings[field.name].verification == "missing" and _type(field) in SUPPORTED_TYPES
        ]
        if not self.configured or not unresolved or not lines:
            return readings, warnings
        supplied = lines[:MAX_LINES]
        transcript = []
        size = 0
        for line in supplied:
            if size + len(line.text) > MAX_TRANSCRIPT:
                break
            entry = {"line_id": line.id, "text": line.text}
            if line.table is not None:
                entry["table"] = line.table.model_dump()
            transcript.append(entry)
            size += len(line.text)
        selected_fields = unresolved[:MAX_FIELDS]
        if not transcript or not selected_fields:
            return readings, warnings
        try:
            selections = self._select(transcript, selected_fields)
            by_id = {line.id: line for line in lines[: len(transcript)]}
            for field in selected_fields:
                selection = selections.get(field.name)
                if not isinstance(selection, dict) or set(selection) != {"line_id", "quote"}:
                    continue
                line = by_id.get(selection["line_id"])
                quote = selection["quote"]
                if (
                    line is None
                    or not isinstance(quote, str)
                    or not quote
                    or quote not in line.text
                ):
                    continue
                candidate = _candidate(quote, line, _type(field))
                if candidate.error is None:
                    readings[field.name] = _reading(
                        [candidate], self.settings.ocr_min_confidence, "schema_model"
                    )
        except Exception:
            warnings.append({"code": "SCHEMA_READER_ERROR"})
        return readings, warnings

    def _select(self, transcript: list[dict], fields: list[ExtractionField]) -> dict:
        questions = [
            {"name": f.name, "type": f.type, "description": f.description, "labels": f.labels}
            for f in fields
        ]
        known_lines = {line["line_id"]: line["text"] for line in transcript}
        known_fields = {field.name for field in fields}

        def validate(selection):
            if not isinstance(selection, dict) or not set(selection) <= known_fields:
                raise ValueError("Invalid schema selection")
            for item in selection.values():
                if not isinstance(item, dict) or set(item) != {"line_id", "quote"}:
                    raise ValueError("Invalid schema selection")
                line_id, quote = item["line_id"], item["quote"]
                if (
                    not isinstance(line_id, str)
                    or not isinstance(quote, str)
                    or not quote
                    or line_id not in known_lines
                    or quote not in known_lines[line_id]
                ):
                    raise ValueError("Ungrounded schema selection")
            return selection

        task = json.dumps({"fields": questions, "lines": transcript}, ensure_ascii=False)
        namespace = str(self.settings.data_dir)
        for index, (provider, model) in enumerate(self._chain()):
            if provider_on_cooldown(provider, model, namespace):
                continue
            try:
                return self._select_provider(
                    provider, model, task, questions, transcript, validate, fallback=index > 0
                )
            except ProviderUnavailable as exc:
                note_provider_failure(provider, model, namespace, exc)
                continue
        raise ProviderUnavailable("No schema provider succeeded")

    def _select_provider(
        self, provider, model, task, questions, transcript, validate, *, fallback=False
    ):
        endpoint = self._endpoint(provider, model)
        max_tokens = self._max_tokens()
        if provider == "gemini":
            if not re.fullmatch(r"gemini-[a-zA-Z0-9._-]+", model):
                raise ValueError("Invalid Gemini model")
            headers = {"x-goog-api-key": self.settings.gemini_api_key}
            body = {
                "contents": [{"role": "user", "parts": [{"text": PROMPT + "\n" + task}]}],
                "generationConfig": {
                    "temperature": 0,
                    "maxOutputTokens": max_tokens,
                    "responseMimeType": "application/json",
                },
            }
        else:
            token = (
                self.settings.vlm_api_key
                if provider == "compatible"
                else self.settings.helmcode_api_key
            )
            headers = {"Authorization": "Bearer " + token} if token else {}
            generation = {"temperature": 0, "max_tokens": max_tokens}
            if provider == "helmcode":
                generation.update(reasoning_effort="none", response_format={"type": "json_object"})
            body = {
                "model": model,
                **generation,
                "messages": [
                    {"role": "system", "content": PROMPT},
                    {"role": "user", "content": task},
                ],
            }
        trace_provider = "vision" if provider == "compatible" else provider

        def call(mark_network_attempt):
            with self.http.session(self.settings.vlm_timeout) as client:
                mark_network_attempt()
                response = client.post(endpoint, headers=headers, json=body)
            record_response(
                trace_provider, response.status_code, retry_after_s=retry_after(response)
            )
            if not response.is_success:
                raise RuntimeError(f"Schema provider returned HTTP {response.status_code}")
            data = response.json()
            record_response(trace_provider, response.status_code, data)
            if provider != "gemini":
                choice = data["choices"][0]
                if choice.get("finish_reason", "stop") != "stop":
                    raise ValueError("Incomplete schema response")
                content = choice["message"]["content"]
            else:
                candidate = data["candidates"][0]
                if candidate.get("finishReason") != "STOP":
                    raise ValueError("Incomplete schema response")
                content = candidate["content"]["parts"][0]["text"]
            if not isinstance(content, str) or len(content) > 20_000:
                raise ValueError("Invalid schema response")
            return validate(json.loads(content))

        result = recorded_call(
            self.settings.data_dir / "provider-journal" / "schema-reader",
            {
                "endpoint": endpoint,
                "model": model,
                "prompt": PROMPT,
                "fields": questions,
                "transcript": transcript,
                "generation": (
                    body["generationConfig"]
                    if provider == "gemini"
                    else {
                        key: value
                        for key, value in body.items()
                        if key not in {"model", "messages"}
                    }
                ),
            },
            call,
            provider=trace_provider,
            model=model,
            operation="schema_selection",
            fallback=fallback,
            force=self.settings.ocr_force_recompute,
        )
        return validate(result)
