"""Release gates for the actual live client. Do not xfail integration gaps.

The browser exercises rendering; these checks enumerate route/header/schema drift,
including actions a smoke visit may not click.
"""

import re
from pathlib import Path

import pytest

from app.main import app

pytestmark = pytest.mark.e2e
FRONTEND = Path(__file__).resolve().parents[3] / "frontend" / "src" / "api"


def canonical(path: str) -> str:
    path = path.split("${query(", 1)[0]
    # Literal query parameters select an operation's options, not a different API route.
    return re.sub(r"\$?\{[^}]+\}", "{}", path).split("?", 1)[0]


def same_route(route: str, call: str) -> bool:
    """`{}` on either side is one segment: the client's `/metrics/{}` covers `/metrics/agents`."""
    a, b = route.split("/"), call.split("/")
    return len(a) == len(b) and all(x == y or "{}" in (x, y) for x, y in zip(a, b, strict=True))


def test_live_client_routes_exist_in_openapi():
    live = (FRONTEND / "live.ts").read_text(encoding="utf-8")
    calls = re.findall(
        r"\b(get|getText|post|put|upload)(?:<.*?)?\(\s*(['\"`])(/.*?)\2",
        live,
        re.S,
    )
    assert len(calls) >= 25, "The route audit no longer recognizes the live client"
    methods = {"getText": "get", "upload": "post"}
    routes = {
        (canonical(path), method)
        for path, operations in app.openapi()["paths"].items()
        for method in operations
    }
    missing = sorted(
        f"{methods.get(method, method).upper()} {path}"
        for method, _, path in calls
        if not any(
            methods.get(method, method) == route_method and same_route(route, canonical(path))
            for route, route_method in routes
        )
    )
    assert not missing, "Live client calls absent from the API:\n" + "\n".join(missing)


def test_live_client_identity_header_matches_backend():
    http = (FRONTEND / "http.ts").read_text(encoding="utf-8")
    parameters = app.openapi()["paths"]["/me"]["get"]["parameters"]
    header = next(item["name"] for item in parameters if item["in"] == "header")
    assert header.lower() in http.lower(), f"Live client must send {header}"


def test_live_client_user_fields_match_api():
    # The wire user is the generated UserOut; test_openapi keeps openapi.json current.
    contract = (FRONTEND / "contracts.ts").read_text(encoding="utf-8")
    assert re.search(r"type User = components\['schemas'\]\['UserOut'\]", contract), (
        "The wire user type must be the API's UserOut"
    )
