"""The connector against a scripted fake API (httpx MockTransport): every failure mode the
challenge ERP has, and the ones it may add on Saturday or Sunday."""

from collections.abc import Callable

import httpx
import pytest

from app.features.sources.http_connector import HttpConnector, SyncError, convert
from app.features.sources.tests.conftest import erp_config

FAST = {
    "retry": {"backoff_seconds": 0.001, "backoff_max_seconds": 0.01, "max_attempts": 4},
    "rate_limit": {"requests_per_second": 1000},
}

ENTRIES = [
    {
        "id": f"AS-{n:05d}",
        "fecha": "31/01/2026",
        "proveedor": "P003",
        "nif": "B30455812",
        "pedido": f"PO-2026-{n:04d}",
        "importe": "12.874,40",
        "estado": "PAGADA" if n == 3 else "PENDIENTE",
    }
    for n in range(45, 0, -1)  # served in reverse: the snapshot must still be sorted
]


def page_xml(page: int, entries: list[dict] = ENTRIES, total: int | None = None) -> bytes:
    total = len(entries) if total is None else total
    chunk = entries[(page - 1) * 20 : page * 20]
    records = "".join(
        "<asiento>" + "".join(f"<{k}>{v}</{k}>" for k, v in e.items()) + "</asiento>" for e in chunk
    )
    pages = (total + 19) // 20
    return (
        '<?xml version="1.0" encoding="ISO-8859-1"?><respuesta><meta>'
        f"<total>{total}</total><paginas>{pages}</paginas></meta>"
        f"<asientos>{records}</asientos></respuesta>"
    ).encode("iso-8859-1")


def error_xml(status: int, code: str, **headers: str) -> httpx.Response:
    body = f"<error><codigo>{code}</codigo><mensaje>x</mensaje></error>".encode()
    return httpx.Response(status, content=body, headers=headers)


def login_xml(token: str) -> httpx.Response:
    return httpx.Response(
        200,
        content=(
            f"<sesion><token>{token}</token><caduca_en_segundos>900</caduca_en_segundos>"
            "<usos_maximos>300</usos_maximos></sesion>"
        ).encode(),
    )


class FakeERP:
    """Logs in, serves pages, and lets a test script a failure for a given call."""

    def __init__(self, fail: Callable[[int, httpx.Request], httpx.Response | None] = None):
        self.fail = fail
        self.calls = 0
        self.logins = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/erp/estado":
            return httpx.Response(200, content=b"<estado><asientos>45</asientos></estado>")
        if request.url.path == "/erp/login":
            self.logins += 1
            return login_xml(f"t{self.logins}")
        self.calls += 1
        if self.fail and (scripted := self.fail(self.calls, request)) is not None:
            return scripted
        return httpx.Response(200, content=page_xml(int(request.url.params["pagina"])))


async def download(fake: FakeERP, **overrides) -> tuple[list[dict], HttpConnector]:
    config = erp_config("http://fake", **{**FAST, **overrides})
    connector = HttpConnector(config, httpx.MockTransport(fake))
    return await connector.download(), connector


async def test_download_maps_converts_and_sorts() -> None:
    rows, c = await download(FakeERP())
    assert [r["entry_id"] for r in rows] == sorted(e["id"] for e in ENTRIES)
    assert rows[0] == {
        "entry_id": "AS-00001",
        "date": "2026-01-31",
        "supplier_id": "P003",
        "nif": "B30455812",
        "purchase_order": "PO-2026-0001",
        "amount": "12874.40",
        "status": "PENDIENTE",
    }
    assert (c.stats.pages, c.stats.logins, c.stats.retries) == (3, 1, 0)
    assert c.stats.status["entries"] == "45"


async def test_source_without_authentication_never_calls_login() -> None:
    fake = FakeERP()
    rows, connector = await download(fake, auth={"type": "none"})
    assert len(rows) == 45
    assert fake.logins == 0
    assert connector.stats.logins == 0


@pytest.mark.parametrize(
    ("kind", "raw", "value"),
    [
        ("decimal_comma", "12.874,40", "12874.40"),
        ("decimal_comma", "1.234.567,00", "1234567.00"),
        ("decimal_comma", "0,5", "0.5"),
        ("decimal_comma", "-3,10", "-3.10"),
        ("decimal_comma", "12874.4", None),  # already converted upstream: flag, never guess
        ("decimal_comma", "12,874.40", None),
        ("decimal_comma", "", None),
        ("date_dmy", "05/02/2026", "2026-02-05"),
        ("date_dmy", "31/02/2026", None),
        ("date_dmy", "2026-02-05", None),
        ("text", "  B30455812 ", "B30455812"),
    ],
)
def test_convert(kind: str, raw: str, value: str | None) -> None:
    assert convert(kind, raw) == value


async def test_unconvertible_values_are_kept_raw_and_flagged() -> None:
    def odd(n: int, request: httpx.Request) -> httpx.Response | None:
        page = int(request.url.params["pagina"])
        entries = [
            {**e, "importe": "12874.4", "fecha": "2026-01-31"} if e["id"] == "AS-00045" else e
            for e in ENTRIES
        ]
        return httpx.Response(200, content=page_xml(page, entries))

    rows, c = await download(FakeERP(odd))
    row = next(r for r in rows if r["entry_id"] == "AS-00045")
    assert row["amount"] == "12874.4" and row["date"] == "2026-01-31"
    assert row["_invalid"] == ["date", "amount"]
    assert c.stats.invalid_values == 2


async def test_transient_error_is_retried() -> None:
    fake = FakeERP(lambda n, _: error_xml(500, "ORA-00600") if n in (1, 3) else None)
    rows, c = await download(fake)
    assert len(rows) == 45
    assert c.stats.transient_errors == {"ORA-00600": 2}
    assert c.stats.retries == 2


async def test_expired_session_logs_in_again() -> None:
    fake = FakeERP(
        lambda n, r: error_xml(401, "SES-401") if r.headers["X-ERP-Token"] == "t1" else None
    )
    rows, c = await download(fake)
    assert len(rows) == 45
    assert c.stats.logins == 2


async def test_token_renewed_before_its_uses_run_out() -> None:
    rows, c = await download(FakeERP(), auth={"max_uses": 2, "renew_margin_uses": 0})
    assert len(rows) == 45
    assert c.stats.logins == 2  # 3 page calls, 2 uses per token


async def test_429_waits_retry_after() -> None:
    fake = FakeERP(
        lambda n, _: error_xml(429, "ERP-429", **{"Retry-After": "0.3"}) if n == 2 else None
    )
    rows, c = await download(fake)
    assert len(rows) == 45
    assert c.stats.rate_limited == 1
    assert c.stats.duration_ms >= 300


@pytest.mark.parametrize(
    "bad",
    [
        httpx.Response(200, content=b"<respuesta><meta><total>45</total>"),  # cut XML
        httpx.Response(200, content=page_xml(1, ENTRIES[:19], total=45)),  # 19 of 20
        httpx.Response(200, content=b"<html>proxy error</html>"),
    ],
    ids=["malformed", "truncated-page", "not-the-api"],
)
async def test_invalid_response_is_retried(bad: httpx.Response) -> None:
    rows, c = await download(FakeERP(lambda n, _: bad if n == 1 else None))
    assert len(rows) == 45
    assert c.stats.invalid_responses == 1


async def test_invalid_response_fails_loudly_when_it_persists() -> None:
    fake = FakeERP(lambda n, _: httpx.Response(200, content=b"<respuesta>"))
    with pytest.raises(SyncError, match="gave up after 4 attempts.*not well-formed"):
        await download(fake)


@pytest.mark.parametrize("total,pages", [(0, 2), (0, -1), (-1, 0), (45, 4), (45, 0)])
async def test_invalid_pagination_metadata_is_retried_without_advancing(total, pages):
    requested_pages = []

    def invalid(n, request):
        page = int(request.url.params["pagina"])
        requested_pages.append(page)
        body = page_xml(page, [] if total <= 0 else ENTRIES, total=total)
        expected_pages = (total + 19) // 20
        body = body.replace(
            f"<paginas>{expected_pages}</paginas>".encode(),
            f"<paginas>{pages}</paginas>".encode(),
        )
        return httpx.Response(200, content=body)

    fake = FakeERP(invalid)
    with pytest.raises(SyncError, match="gave up after 4 attempts.*pagination"):
        await download(fake)
    assert fake.calls == 4
    assert requested_pages == [1] * 4


@pytest.mark.parametrize("pages", [0, 1])
async def test_empty_source_accepts_zero_or_one_empty_page(pages):
    body = page_xml(1, []).replace(b"<paginas>0</paginas>", f"<paginas>{pages}</paginas>".encode())
    fake = FakeERP(lambda n, request: httpx.Response(200, content=body))
    rows, connector = await download(fake)
    assert rows == []
    assert connector.stats.pages == fake.calls == 1


async def test_timeouts_are_retried_then_abort() -> None:
    def slow(n: int, request: httpx.Request) -> None:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(SyncError, match="gave up after 4 attempts.*timeout"):
        await download(FakeERP(slow))


async def test_consecutive_failures_abort_the_sync() -> None:
    fake = FakeERP(lambda n, _: error_xml(500, "ORA-00600") if n >= 2 else None)
    with pytest.raises(SyncError, match="gave up after 4 attempts.*ORA-00600"):
        await download(fake)
    assert fake.calls == 5  # page 1, then four attempts at page 2


async def test_client_error_is_not_retried() -> None:
    fake = FakeERP(lambda n, _: error_xml(400, "ERP-400"))
    with pytest.raises(SyncError, match="HTTP 400 ERP-400"):
        await download(fake)
    assert fake.calls == 1


async def test_source_changing_mid_download_aborts() -> None:
    def grows(n: int, request: httpx.Request) -> httpx.Response | None:
        if n == 2:
            return httpx.Response(200, content=page_xml(2, ENTRIES, total=46))
        return None

    with pytest.raises(SyncError, match="changed during the download"):
        await download(FakeERP(grows))


async def test_duplicate_keys_abort() -> None:
    doubled = ENTRIES[:20] + ENTRIES[:20] + ENTRIES[40:]
    fake = FakeERP(lambda n, r: httpx.Response(200, content=page_xml(n, doubled)))
    with pytest.raises(SyncError, match="appears twice"):
        await download(fake)


async def test_broken_status_resource_does_not_block_the_data() -> None:
    def no_status(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/erp/estado":
            return error_xml(404, "ERP-404")
        return FakeERP()(request)

    connector = HttpConnector(erp_config("http://fake", **FAST), httpx.MockTransport(no_status))
    assert len(await connector.download()) == 45
    assert "HTTP 404" in connector.stats.status["error"]
