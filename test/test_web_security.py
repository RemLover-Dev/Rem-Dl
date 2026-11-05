"""Regression tests for the WSGI access boundary (no bypass fixture)."""
import json

import pytest

werkzeug = pytest.importorskip("werkzeug")
from werkzeug.test import Client
from werkzeug.wrappers import Response

from core.web_security import AccessGuard


def _app(environ, start_response):
    body = b"protected"
    start_response("200 OK", [("Content-Type", "text/plain"), ("Content-Length", str(len(body)))])
    return [body]


def _client(token="T" * 40, hosts=None):
    guard = AccessGuard(_app, token=token, hosts=hosts)
    return guard, Client(guard, Response)


def test_missing_and_invalid_credentials_are_rejected_without_caching():
    guard, client = _client()
    response = client.get("/api/private")
    assert response.status_code == 401
    assert response.headers["Cache-Control"] == "no-store"

    response = client.get("/api/private", headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401


def test_valid_bearer_token_reaches_application():
    token = "T" * 40
    _, client = _client(token=token)
    response = client.get("/api/private", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.data == b"protected"


def test_auth_exchange_sets_httponly_strict_cookie_and_no_store():
    token = "T" * 40
    _, client = _client(token=token)
    response = client.post("/auth", data=json.dumps({"token": token}),
                           content_type="application/json")
    assert response.status_code == 204
    cookie = response.headers.get("Set-Cookie", "")
    assert "HttpOnly" in cookie
    assert "SameSite=Strict" in cookie
    assert response.headers["Cache-Control"] == "no-store"


def test_auth_rejects_oversized_body():
    _, client = _client()
    response = client.post("/auth", data=b"x" * 4097,
                           content_type="application/json")
    assert response.status_code == 413


def test_untrusted_host_origin_and_cross_site_requests_are_rejected():
    _, client = _client()
    token = "T" * 40
    headers = {"Authorization": f"Bearer {token}"}

    assert client.get("/api/private", headers={**headers, "Host": "attacker.invalid"}).status_code == 403
    assert client.get("/api/private", headers={**headers, "Origin": "https://attacker.invalid"}).status_code == 403
    assert client.get("/api/private", headers={**headers, "Sec-Fetch-Site": "cross-site"}).status_code == 403


def test_auth_works_behind_tls_terminating_proxy():
    """The app sees http while the browser origin is https — same site.

    This was the 'valid token rejected' bug when running headless behind a
    reverse proxy: every login POST got 403 and the page said invalid token.
    """
    token = "T" * 40
    _, client = _client(token=token, hosts={"example.com"})

    r = client.post("/auth", data=json.dumps({"token": token}),
                    content_type="application/json",
                    headers={"Host": "example.com", "Origin": "https://example.com"})
    assert r.status_code == 204
    assert "Secure" in r.headers.get("Set-Cookie", "")

    # a different site with the same scheme is still cross-site
    r = client.post("/auth", data=json.dumps({"token": token}),
                    content_type="application/json",
                    headers={"Host": "example.com", "Origin": "https://evil.example"})
    assert r.status_code == 403


def test_generated_token_is_marked_but_explicit_token_is_not(monkeypatch):
    monkeypatch.delenv("REMS_ACCESS_TOKEN", raising=False)
    generated = AccessGuard(_app)
    assert generated.token_generated is True
    assert len(generated.token) >= 32

    explicit = AccessGuard(_app, token="X" * 40)
    assert explicit.token_generated is False
