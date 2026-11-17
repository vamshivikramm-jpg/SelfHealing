"""Unit tests for the dashboard login (viewer/auth.py and its wiring).

No HTTP client needed: the gate, the middleware and the endpoints are
called directly with Starlette requests built from ASGI scopes.
"""

import asyncio
import importlib
import json
import sys

import pytest

pytest.importorskip("fastapi")
from starlette.requests import Request  # noqa: E402

from viewer.auth import COOKIE, LOCKOUT_S, MAX_FAILURES, Auth, safe_next  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_default_credentials_are_root_toor(monkeypatch):
    for k in ("VIEWER_USER", "VIEWER_PASSWORD", "VIEWER_AUTH"):
        monkeypatch.delenv(k, raising=False)
    auth = Auth()
    assert auth.enabled and auth.check("root", "toor")
    assert not auth.check("root", "wrong") and not auth.check("admin", "toor") and not auth.check("", "")


def test_credentials_from_environment(monkeypatch):
    monkeypatch.setenv("VIEWER_USER", "ops")
    monkeypatch.setenv("VIEWER_PASSWORD", "s3cret")
    auth = Auth()
    assert auth.check("ops", "s3cret") and not auth.check("root", "toor")


def test_login_session_and_logout():
    clock = Clock()
    auth = Auth("root", "toor", True, session_hours=1, clock=clock)
    r = auth.login("root", "toor", "10.0.0.1")
    assert r["ok"] and len(r["token"]) >= 40
    assert auth.session(r["token"])["user"] == "root"
    assert auth.session("forged-token") is None
    auth.logout(r["token"])
    assert auth.session(r["token"]) is None


def test_session_expires():
    clock = Clock()
    auth = Auth("root", "toor", True, session_hours=1, clock=clock)
    token = auth.login("root", "toor", "a")["token"]
    clock.t += 3599
    assert auth.session(token)
    clock.t += 2
    assert auth.session(token) is None


def test_lockout_after_repeated_failures():
    clock = Clock()
    auth = Auth("root", "toor", True, clock=clock)
    for _ in range(MAX_FAILURES):
        assert auth.login("root", "nope", "1.2.3.4")["status"] == 401
    locked = auth.login("root", "toor", "1.2.3.4")  # even the right password waits
    assert not locked["ok"] and locked["status"] == 429
    assert auth.login("root", "toor", "5.6.7.8")["ok"]  # other addresses unaffected
    clock.t += LOCKOUT_S + 1
    assert auth.login("root", "toor", "1.2.3.4")["ok"]


def test_gate_protects_everything_but_login_health_static():
    auth = Auth("root", "toor", True)
    for path in ("/login", "/api/login", "/api/health", "/static/css/dashboard.css"):
        assert auth.gate(path, "", None) is None
    assert auth.gate("/api/overview", "", None) == {"status": 401, "json": {"detail": "Sign in required."}}
    assert auth.gate("/api/sim/failure", "", None)["status"] == 401
    assert auth.gate("/", "x=1", None) == {"redirect": "/login?next=%2F%3Fx%3D1"}
    token = auth.login("root", "toor", "a")["token"]
    assert auth.gate("/api/overview", "", token) is None
    assert Auth("root", "toor", False).gate("/api/overview", "", None) is None


def test_safe_next_blocks_open_redirects():
    assert safe_next("/#control") == "/#control"
    for bad in (None, "", "https://evil.example", "//evil.example", "/\\evil", "javascript:alert(1)"):
        assert safe_next(bad) == "/"


def _request(path="/", method="GET", cookie=None, query=""):
    headers = [(b"cookie", f"{COOKIE}={cookie}".encode())] if cookie else []
    return Request({"type": "http", "method": method, "path": path, "query_string": query.encode(), "headers": headers,
                    "client": ("10.0.0.9", 5555), "server": ("test", 80), "scheme": "http", "root_path": ""})


@pytest.fixture()
def viewer(monkeypatch):
    for k in ("VIEWER_MODE", "VIEWER_AUTH", "VIEWER_USER", "VIEWER_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    sys.modules.pop("viewer.app", None)
    app = importlib.import_module("viewer.app")
    yield app
    sys.modules.pop("viewer.app", None)


def test_middleware_blocks_and_redirects(viewer):
    async def call_next(_):
        return "handler"

    run = asyncio.run
    api = run(viewer._require_login(_request("/api/overview"), call_next))
    assert api.status_code == 401
    page = run(viewer._require_login(_request("/"), call_next))
    assert page.status_code == 303 and page.headers["location"] == "/login?next=%2F"
    assert run(viewer._require_login(_request("/api/health"), call_next)) == "handler"


def test_login_endpoint_sets_secure_cookie_and_logout_clears_it(viewer):
    run = asyncio.run
    bad = run(viewer.login(viewer.LoginRequest(username="root", password="x"), _request("/api/login", "POST")))
    assert bad.status_code == 401 and "set-cookie" not in bad.headers
    ok = run(viewer.login(viewer.LoginRequest(username="root", password="toor", next="https://evil.example"),
                          _request("/api/login", "POST")))
    assert ok.status_code == 200 and json.loads(ok.body) == {"user": "root", "next": "/"}
    cookie = ok.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE}=") and "HttpOnly" in cookie and "SameSite=strict" in cookie
    token = cookie.split(";")[0].split("=", 1)[1]
    me = run(viewer.me(_request("/api/me", cookie=token)))
    assert me["user"] == "root"

    async def call_next(_):
        return "handler"
    assert run(viewer._require_login(_request("/api/overview", cookie=token), call_next)) == "handler"
    out = run(viewer.logout(_request("/api/logout", "POST", cookie=token)))
    assert out.status_code == 200 and f'{COOKIE}=""' in out.headers["set-cookie"]
    assert run(viewer._require_login(_request("/api/overview", cookie=token), call_next)).status_code == 401
