"""Sankaku watcher adapter: sankakuapi.com /posts (Bearer token, or
SANKA_LOGIN/SANKA_PASSWORD login)."""

import os

from core.watchers.moe import fields, keep, query
from core.watchers.net import api_settings, get, post

LIMIT = 40
API = "https://sankakuapi.com"
NAME = "Sankaku"


def _headers():
    h = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
         "Accept": "application/json",
         "Referer": "https://www.sankakucomplex.com/"}
    token = os.getenv("SANKA_ACCESS_TOKEN", "")
    if not token:
        cfg = api_settings()
        login = cfg.get("sanka_login") or ""
        pw = cfg.get("sanka_password") or ""
        if login and pw:
            for url in (f"{API}/auth/token",
                        "https://login.sankakucomplex.com/auth/token"):
                try:
                    r = post(url, json={"login": login, "password": pw})
                    if r.ok:
                        data = r.json() or {}
                        token = data.get("token") or data.get("access_token") or ""
                        if token:
                            break
                except Exception:
                    pass
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def fetch(api_tag, pid):
    """One page (1-based; pid stays 0-based)."""
    resp = get(f"{API}/posts",
               params={"limit": LIMIT, "page": pid + 1, "tags": api_tag},
               headers=_headers())
    if resp.status_code in (401, 403, 429):
        raise RuntimeError(f"HTTP {resp.status_code}")
    resp.raise_for_status()
    posts = resp.json() or []
    return posts if isinstance(posts, list) else []
