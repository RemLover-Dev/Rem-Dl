"""Yande.re watcher adapter: moebooru post.json (no auth)."""

from core.watchers.moe import fields, keep, query
from core.watchers.net import get

LIMIT = 100


def fetch(api_tag, pid):
    """One page (moebooru pages are 1-based; pid stays 0-based)."""
    resp = get("https://yande.re/post.json",
               params={"tags": api_tag, "page": pid + 1, "limit": LIMIT})
    if resp.status_code in (403, 429):
        raise RuntimeError(f"yande HTTP {resp.status_code}")
    resp.raise_for_status()
    return resp.json() or []
