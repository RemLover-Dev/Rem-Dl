"""REMS_BASE_PATH subpath deployments and proxy-aware host checks."""
import json

import pytest

werkzeug = pytest.importorskip("werkzeug")
from werkzeug.test import Client
from werkzeug.wrappers import Response

from core.web_security import AccessGuard, normalize_base_path


def _app(environ, start_response):
    body = b"protected"
    start_response("200 OK", [("Content-Type", "text/plain"), ("Content-Length", str(len(body)))])
    return [body]


def _client(token="T" * 40, hosts=None, base_path=None, monkeypatch=None):
    if base_path is not None and monkeypatch is not None:
        monkeypatch.setenv("REMS_BASE_PATH", base_path)
    guard = AccessGuard(_app, token=token, hosts=hosts)
    return guard, Client(guard, Response)


def test_normalize_base_path():
    assert normalize_base_path("") == ""
    assert normalize_base_path("/") == ""
    assert normalize_base_path("remdl") == "/remdl"
    assert normalize_base_path("/remdl/") == "/remdl"
    assert normalize_base_path(" /remdl/ ") == "/remdl"


def test_base_path_scopes_cookie_and_login_page(monkeypatch):
    guard, client = _client(base_path="/remdl/", monkeypatch=monkeypatch)
    response = client.post("/auth", data=json.dumps({"token": "T" * 40}),
                           content_type="application/json")
    assert response.status_code == 204
    assert "Path=/remdl" in response.headers.get("Set-Cookie", "")

    page = client.get("/").data.decode()
    assert 'const BASE = "/remdl"' in page
    assert "fetch(BASE + '/auth'" in page


def test_root_mode_keeps_cookie_at_root_and_relative_login(monkeypatch):
    guard, client = _client(base_path="", monkeypatch=monkeypatch)
    response = client.post("/auth", data=json.dumps({"token": "T" * 40}),
                           content_type="application/json")
    assert response.status_code == 204
    assert "Path=/" in response.headers.get("Set-Cookie", "")
    # fresh client: no cookie → the login page is served with an empty BASE
    _, fresh = _client(base_path="", monkeypatch=monkeypatch)
    assert 'const BASE = ""' in fresh.get("/").data.decode()


def test_host_matches_with_or_without_port(monkeypatch):
    guard, client = _client(hosts=("rems.example.com",), base_path="", monkeypatch=monkeypatch)
    assert client.get("/api/x", headers={"Host": "rems.example.com:8443"}).status_code == 401

    guard, client = _client(hosts=("rems.example.com:8443",), base_path="", monkeypatch=monkeypatch)
    assert client.get("/api/x", headers={"Host": "rems.example.com"}).status_code == 401


def test_blocked_host_is_logged(capsys, monkeypatch):
    import re
    guard, client = _client(hosts=("rems.example.com",), base_path="", monkeypatch=monkeypatch)
    response = client.get("/api/x", headers={"Host": "evil.example.com"})
    assert response.status_code == 403
    out = capsys.readouterr().out
    assert re.search(r"\bevil\.example\.com\b", out) and "REMS_ALLOWED_HOSTS" in out


def test_origin_hostname_is_matched_against_allowlist(monkeypatch):
    guard, client = _client(hosts=("rems.example.com",), base_path="", monkeypatch=monkeypatch)
    response = client.post("/auth", data=json.dumps({"token": "T" * 40}),
                           content_type="application/json",
                           headers={"Origin": "https://rems.example.com:8443",
                                    "Host": "rems.example.com"})
    assert response.status_code == 204

    guard, client = _client(hosts=("rems.example.com",), base_path="", monkeypatch=monkeypatch)
    response = client.post("/auth", data=json.dumps({"token": "T" * 40}),
                           content_type="application/json",
                           headers={"Origin": "https://evil.example.com",
                                    "Host": "rems.example.com"})
    assert response.status_code == 403


def test_index_injects_base_path(monkeypatch, bypass_access_guard):
    Rems_Dl = pytest.importorskip("Rems_Dl")
    monkeypatch.setattr(Rems_Dl.access_guard, "base_path", "/remdl")
    Rems_Dl.app.config["TESTING"] = True
    with Rems_Dl.app.test_client() as client:
        html = client.get("/").data.decode()
    assert 'window.REMS_BASE="/remdl"' in html

    monkeypatch.setattr(Rems_Dl.access_guard, "base_path", "")
    with Rems_Dl.app.test_client() as client:
        html = client.get("/").data.decode()
    assert "window.REMS_BASE" not in html
