"""Safebooru watcher adapter: same dapi json as gelbooru (rating
metatags work via the same negation trick)."""

from core.watchers.gelbooru import query
from core.watchers.net import get, std_fields

API = "https://safebooru.org/index.php"
LIMIT = 100
NAME = "Safebooru"


def fetch(api_tag, pid):
    """One safebooru page (0-based pid)."""
    params = {"page": "dapi", "s": "post", "q": "index", "tags": api_tag,
              "pid": pid, "limit": LIMIT, "json": 1}
    resp = get(API, params=params, headers={"User-Agent": "Mozilla/5.0"})
    if resp.status_code in (401, 403, 429):
        raise RuntimeError(f"HTTP {resp.status_code}")
    resp.raise_for_status()
    # safebooru answers a bare list; gelbooru wraps it in {"post": [...]}
    data = resp.json() or []
    if isinstance(data, dict):
        data = data.get("post") or []
    return data if isinstance(data, list) else [data]


fields = std_fields
