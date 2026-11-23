"""Konachan watcher adapter: moebooru post.json."""

import hashlib

from core.watchers.moe import fields, keep, query
from core.watchers.net import api_settings, get

LIMIT = 100
NAME = "Konachan"


def fetch(api_tag, pid):
    """One page (moebooru pages are 1-based; pid stays 0-based)."""
    params = {"tags": api_tag, "page": pid + 1, "limit": LIMIT}
    cfg = api_settings()
    u, p = cfg.get("konachan_login") or "", cfg.get("konachan_password") or ""
    if u and p:
        # same sha1 salt trick as the konachan downloader
        params["login"] = u
        # the moebooru API mandates sha1(salt+password) for auth; this is a
        # protocol signature, not local password storage, so sha1 cannot change
        params["password_hash"] = hashlib.sha1(
            f"So-I-Heard-You-Like-Mupkids-?--{p}--".encode()).hexdigest()
    resp = get("https://konachan.com/post.json", params=params)
    if resp.status_code in (403, 429):
        raise RuntimeError(f"HTTP {resp.status_code}")
    resp.raise_for_status()
    return resp.json() or []
