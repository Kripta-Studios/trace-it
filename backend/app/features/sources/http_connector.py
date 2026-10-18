"""Download a whole paginated HTTP source into memory, or fail without partial results.

The connector is generic: everything about a concrete API (paths, auth, pagination, field
names, value formats, error codes, limits) comes from the source's entry in the process
pack's `sources.json` (see `docs/sources-http.md`). Only the options the challenge ERP needs
are implemented: form login returning a token, page-number pagination with a total in the
response, XML bodies. A new kind of API extends this code behind the same configuration.

Rules never call this: a sync stores the rows as a new `Source` snapshot and the engine
reads snapshots only.
"""

import asyncio
import os
import random
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

import httpx
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EnvValue(_Strict):
    """A value read from an environment variable (or `.env`) at sync time."""

    env: str
    default: str | None = None


class FormatConfig(_Strict):
    type: Literal["xml"] = "xml"
    encoding: str = "utf-8"
    error_code_path: str = "codigo"  # element holding the API's own error code, if any


class AuthConfig(_Strict):
    type: Literal["form_token"] = "form_token"
    login_path: str
    credentials: dict[str, str]  # form field -> environment variable holding its value
    token_path: str
    token_header: str
    lifetime_seconds: float
    max_uses: int
    lifetime_path: str | None = None  # if the login response states them, the lower value wins
    max_uses_path: str | None = None
    renew_margin_seconds: float = 60
    renew_margin_uses: int = 10
    expired_codes: list[str] = ["SES-401"]

    @model_validator(mode="after")
    def _margins_below_limits(self) -> "AuthConfig":
        if not 0 <= self.renew_margin_uses < self.max_uses:
            raise ValueError("renew_margin_uses must be below max_uses")
        if not 0 <= self.renew_margin_seconds < self.lifetime_seconds:
            raise ValueError("renew_margin_seconds must be below lifetime_seconds")
        return self


class NoAuthConfig(_Strict):
    type: Literal["none"]


class PaginationConfig(_Strict):
    path: str
    page_param: str
    first_page: int = 1
    page_size: int = Field(gt=0)
    records_path: str
    total_path: str
    # Stop condition: the page count the API reports at this path (the only one implemented).
    pages_path: str


class FieldConfig(_Strict):
    source: str  # element name in the API record
    convert: Literal["text", "decimal_comma", "date_dmy"] = "text"


class RetryConfig(_Strict):
    max_attempts: int = Field(default=6, ge=1)  # per request; then the sync aborts (breaker)
    retry_statuses: list[int] = [500, 502, 503, 504]
    transient_codes: list[str] = ["ORA-00600"]
    backoff_seconds: float = 0.2
    backoff_max_seconds: float = 5


class RateLimitConfig(_Strict):
    requests_per_second: float = Field(default=6, gt=0)
    max_retry_after_seconds: float = 30


class TimeoutConfig(_Strict):
    connect_seconds: float = 3
    read_seconds: float = 15


class StatusConfig(_Strict):
    """An unauthenticated health resource whose fields are recorded with the snapshot, e.g.
    whether the batch 2 update is loaded."""

    path: str
    fields: dict[str, str]  # name in the trace -> element path


class HttpSourceConfig(_Strict):
    type: Literal["http"]
    base_url: EnvValue
    format: FormatConfig
    auth: AuthConfig | NoAuthConfig
    pagination: PaginationConfig
    key: str
    fields: dict[str, FieldConfig]
    retry: RetryConfig = RetryConfig()
    rate_limit: RateLimitConfig = RateLimitConfig()
    timeouts: TimeoutConfig = TimeoutConfig()
    status: StatusConfig | None = None

    @model_validator(mode="after")
    def _key_is_a_field(self) -> "HttpSourceConfig":
        if self.key not in self.fields:
            raise ValueError(f"key {self.key!r} is not one of the fields")
        return self


class SourcesFile(_Strict):
    sources: dict[str, HttpSourceConfig]


def env_value(value: EnvValue) -> str:
    """The process environment wins; then `.env` in the working directory or its parent."""
    if value.env in os.environ:
        return os.environ[value.env]
    for path in (Path(".env"), Path("../.env")):
        if path.is_file():
            found = dotenv_values(path).get(value.env)
            if found is not None:
                return found
    if value.default is None:
        raise SyncError(f"environment variable {value.env} is not set (see .env.example)")
    return value.default


class SyncError(Exception):
    """The download failed; nothing may be stored."""


class InvalidResponse(Exception):
    """A response that cannot be accepted as data. Retried like a transient error."""


# --- value conversion -------------------------------------------------------------------

_DECIMAL_COMMA = re.compile(r"-?\d{1,3}(\.\d{3})*(,\d+)?|-?\d+(,\d+)?")


def convert(kind: str, raw: str) -> str | None:
    """The value in the format the rules expect, or None if it cannot be converted."""
    raw = raw.strip()
    if kind == "text":
        return raw
    if kind == "decimal_comma":  # 12.874,40 -> 12874.40
        if not _DECIMAL_COMMA.fullmatch(raw):
            return None
        try:
            return str(Decimal(raw.replace(".", "").replace(",", ".")))
        except InvalidOperation:
            return None
    if kind == "date_dmy":  # 31/01/2026 -> 2026-01-31
        try:
            return datetime.strptime(raw, "%d/%m/%Y").date().isoformat()
        except ValueError:
            return None
    raise ValueError(kind)


def map_record(record: ET.Element, fields: dict[str, FieldConfig]) -> dict[str, Any]:
    """One API record as a source row. A value that does not convert is kept raw and its
    column is listed in `_invalid`, so a rule or a person can see it."""
    row: dict[str, Any] = {}
    invalid = []
    for column, spec in fields.items():
        element = record.find(spec.source)
        if element is None:
            raise InvalidResponse(f"record without <{spec.source}>")
        raw = element.text or ""
        value = convert(spec.convert, raw)
        if value is None:
            invalid.append(column)
            value = raw
        row[column] = value
    if invalid:
        row["_invalid"] = invalid
    return row


# --- client -----------------------------------------------------------------------------


@dataclass
class Stats:
    requests: int = 0
    pages: int = 0
    retries: int = 0
    logins: int = 0
    rate_limited: int = 0  # 429 responses
    transient_errors: dict[str, int] = field(default_factory=dict)  # code or status -> count
    timeouts: int = 0
    invalid_responses: int = 0
    invalid_values: int = 0
    duration_ms: int = 0
    status: dict[str, str] = field(default_factory=dict)


class _Limiter:
    """Spaces request starts at least 1/rate apart. The server counts every request in a
    sliding second, rejected ones included, so this counts every request too."""

    def __init__(self, rate: float) -> None:
        self.interval = 1 / rate
        self.next_at = 0.0

    async def wait(self) -> None:
        now = time.monotonic()
        if self.next_at > now:
            await asyncio.sleep(self.next_at - now)
        self.next_at = max(now, self.next_at) + self.interval

    def pause(self, seconds: float) -> None:
        self.next_at = max(self.next_at, time.monotonic() + seconds)


def _parse(body: bytes, encoding: str) -> ET.Element:
    try:
        text = body.decode(encoding)
        # The document is already text: drop the declaration so its encoding is not re-applied.
        return ET.fromstring(re.sub(r"^\s*<\?xml[^>]*\?>", "", text))
    except (UnicodeDecodeError, ET.ParseError) as e:
        raise InvalidResponse(f"not well-formed XML: {e}") from e


def _number(root: ET.Element, path: str) -> int:
    try:
        return int((root.findtext(path) or "").strip())
    except ValueError as e:
        raise InvalidResponse(f"<{path}> is missing or not a number") from e


class HttpConnector:
    def __init__(self, config: HttpSourceConfig, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        self.base_url = env_value(config.base_url).rstrip("/")
        self.stats = Stats()
        self._limiter = _Limiter(config.rate_limit.requests_per_second)
        self._transport = transport
        self._token: str | None = None
        self._token_deadline = 0.0
        self._token_uses_left = 0

    async def download(self) -> list[dict[str, Any]]:
        """Every record, sorted by the key. Raises SyncError; never returns partial data."""
        started = time.monotonic()
        t = self.config.timeouts
        timeout = httpx.Timeout(t.read_seconds, connect=t.connect_seconds)
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url, timeout=timeout, transport=self._transport
            ) as self._http:
                if self.config.status:
                    await self._read_status()
                rows = await self._all_pages()
        finally:
            self.stats.duration_ms = int((time.monotonic() - started) * 1000)
        return rows

    async def _read_status(self) -> None:
        """Informative only: a broken health resource must not block the data."""
        try:
            root = await self._request("GET", self.config.status.path, authenticated=False)
        except SyncError as e:
            self.stats.status = {"error": str(e)}
            return
        self.stats.status = {
            name: (root.findtext(path) or "").strip()
            for name, path in self.config.status.fields.items()
        }

    async def _all_pages(self) -> list[dict[str, Any]]:
        p = self.config.pagination
        page, pages, total = p.first_page, None, None
        rows: dict[str, dict[str, Any]] = {}
        while pages is None or page < p.first_page + pages:
            page_total, page_pages, records = await self._request(
                "GET", p.path, params={p.page_param: page}, parse=self._page_parser(page)
            )
            if total is None:
                total, pages = page_total, page_pages
            elif (page_total, page_pages) != (total, pages):
                raise SyncError(
                    f"the source changed during the download: page {page} reports "
                    f"{page_total} records in {page_pages} pages, page {p.first_page} "
                    f"reported {total} in {pages}"
                )
            for row in records:
                key = row[self.config.key]
                if key in rows:
                    raise SyncError(f"key {key!r} appears twice (page {page})")
                rows[key] = row
            self.stats.pages += 1
            page += 1
        if len(rows) != total:
            raise SyncError(f"downloaded {len(rows)} records, the source reports {total}")
        self.stats.invalid_values = sum(len(r.get("_invalid", [])) for r in rows.values())
        return [rows[k] for k in sorted(rows)]

    def _page_parser(self, page: int) -> Callable[[ET.Element], tuple[int, int, list[dict]]]:
        """A page must hold exactly the records its position implies: a short page is a
        truncated response and is retried like any other invalid response."""
        p = self.config.pagination

        def parse(root: ET.Element) -> tuple[int, int, list[dict]]:
            total, pages = _number(root, p.total_path), _number(root, p.pages_path)
            expected_pages = (total + p.page_size - 1) // p.page_size
            valid_pages = {0, 1} if total == 0 else {expected_pages}
            if total < 0 or pages not in valid_pages:
                raise InvalidResponse(
                    f"invalid pagination: {total} records in {pages} pages "
                    f"with page size {p.page_size}"
                )
            records = [map_record(r, self.config.fields) for r in root.findall(p.records_path)]
            expected = max(0, min(p.page_size, total - (page - p.first_page) * p.page_size))
            if len(records) != expected:
                raise InvalidResponse(
                    f"page {page} has {len(records)} records, expected {expected}"
                )
            return total, pages, records

        return parse

    # --- one request, with every recovery -------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, str] | None = None,
        authenticated: bool = True,
        parse: Callable[[ET.Element], Any] = lambda root: root,
    ) -> Any:
        r = self.config.retry
        last_error = ""
        for attempt in range(1, r.max_attempts + 1):
            if attempt > 1:
                self.stats.retries += 1
            headers = {}
            use_auth = authenticated and self.config.auth.type != "none"
            if use_auth:
                await self._ensure_token()
                headers[self.config.auth.token_header] = self._token
                self._token_uses_left -= 1  # the server spends a use even on a failed call
            await self._limiter.wait()
            self.stats.requests += 1
            try:
                response = await self._http.request(
                    method, path, params=params, data=data, headers=headers
                )
            except httpx.TimeoutException:
                self.stats.timeouts += 1
                last_error = "timeout"
                await self._backoff(attempt)
                continue
            except httpx.TransportError as e:
                self._count(type(e).__name__)
                last_error = f"transport error: {e!r}"
                await self._backoff(attempt)
                continue

            code = self._error_code(response)
            if response.status_code == 429:
                self.stats.rate_limited += 1
                last_error = "429"
                self._limiter.pause(self._retry_after(response))
                continue
            if (
                response.status_code == 401
                and use_auth
                and (code in self.config.auth.expired_codes or code is None)
            ):
                self._token = None  # log in again on the next attempt
                last_error = code or "401"
                continue
            if response.status_code in r.retry_statuses or code in r.transient_codes:
                self._count(code or str(response.status_code))
                last_error = code or str(response.status_code)
                await self._backoff(attempt)
                continue
            if response.status_code != 200:
                raise SyncError(
                    f"{method} {path} {params or ''}: HTTP {response.status_code} {code or ''}"
                    " (not retryable)"
                )
            try:
                return parse(_parse(response.content, self.config.format.encoding))
            except InvalidResponse as e:
                self.stats.invalid_responses += 1
                last_error = f"invalid response: {e}"
                await self._backoff(attempt)
                continue
        raise SyncError(
            f"{method} {path} {params or ''}: gave up after {r.max_attempts} attempts "
            f"(last: {last_error})"
        )

    def _count(self, code: str) -> None:
        self.stats.transient_errors[code] = self.stats.transient_errors.get(code, 0) + 1

    def _error_code(self, response: httpx.Response) -> str | None:
        if response.status_code == 200:
            return None
        try:
            root = _parse(response.content, self.config.format.encoding)
        except InvalidResponse:
            return None
        found = root.findtext(self.config.format.error_code_path)
        return found.strip() if found else None

    def _retry_after(self, response: httpx.Response) -> float:
        try:
            seconds = float(response.headers.get("Retry-After", "1"))
        except ValueError:  # an HTTP date: not worth parsing for a one-second limit
            seconds = 1
        return min(max(seconds, 0), self.config.rate_limit.max_retry_after_seconds)

    async def _backoff(self, attempt: int) -> None:
        r = self.config.retry
        ceiling = min(r.backoff_max_seconds, r.backoff_seconds * 2 ** (attempt - 1))
        await asyncio.sleep(random.uniform(0, ceiling))  # full jitter

    # --- session ------------------------------------------------------------------------

    async def _ensure_token(self) -> None:
        a = self.config.auth
        if a.type == "none":
            return
        if (
            self._token
            and time.monotonic() < self._token_deadline
            and self._token_uses_left > a.renew_margin_uses
        ):
            return
        credentials = {
            form_field: env_value(EnvValue(env=variable))
            for form_field, variable in a.credentials.items()
        }
        root = await self._request("POST", a.login_path, data=credentials, authenticated=False)
        token = (root.findtext(a.token_path) or "").strip()
        if not token:
            raise SyncError(f"login response without <{a.token_path}>")
        lifetime, uses = a.lifetime_seconds, a.max_uses
        if a.lifetime_path and root.findtext(a.lifetime_path):
            lifetime = min(lifetime, _number(root, a.lifetime_path))
        if a.max_uses_path and root.findtext(a.max_uses_path):
            uses = min(uses, _number(root, a.max_uses_path))
        self._token = token
        self._token_deadline = time.monotonic() + lifetime - a.renew_margin_seconds
        self._token_uses_left = uses
        self.stats.logins += 1
