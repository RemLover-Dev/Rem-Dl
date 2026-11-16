"""Konachan watcher adapter: moebooru post.json."""

import hashlib
import os

from core.watchers.moe import fields, keep, query
from core.watchers.net import get

LIMIT = 100


def fetch(api_tag, pid):
    """One page (moebooru pages are 1-based; pid stays 0-based)."""
    params = {"tags": api_tag, "page": pid + 1, "limit": LIMIT}
    u, p = os.getenv("KONACHAN_USERNAME", ""), os.getenv("KONACHAN_PASSWORD", "")
    if u and p:
        # same sha1 salt trick as the konachan downloader
        params["login"] = u
        params["password_hash"] = hashlib.sha1(
            f"So-I-Heard-You-Like-Mupkids-?--{p}--".encode()).hexdigest()
    resp = get("https://konachan.com/post.json", params=params)
    if resp.status_code in (403, 429):
        raise RuntimeError(f"konachan HTTP {resp.status_code}")
    resp.raise_for_status()
    return resp.json() or []
