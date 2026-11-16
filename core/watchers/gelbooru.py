"""Gelbooru watcher adapter: dapi json."""

import os

from core.watchers.net import get, std_fields
from workers.gelbooru import build_query

API = "https://gelbooru.com/index.php"
LIMIT = 100


def query(tags, ratings):
    # dapi has no comma-OR for ratings; the negation trick ORs them
    return build_query(" ".join(str(t) for t in tags if str(t).strip()),
                       " ".join(str(r) for r in (ratings or [])))[0]


def fetch(api_tag, pid):
    """One gelbooru page (0-based pid)."""
    params = {"page": "dapi", "s": "post", "q": "index", "tags": api_tag,
              "pid": pid, "limit": LIMIT, "json": 1}
    api_key = os.getenv("GELBOORU_API_KEY", "")
    user_id = os.getenv("GELBOORU_USER_ID", "")
    if api_key and user_id:
        params["api_key"] = api_key
        params["user_id"] = user_id
    resp = get(API, params=params)
    if resp.status_code in (401, 403, 429):
        raise RuntimeError(f"gelbooru HTTP {resp.status_code}")
    resp.raise_for_status()
    posts = (resp.json() or {}).get("post") or []
    # gelbooru returns a bare dict for a single-result page
    return posts if isinstance(posts, list) else [posts]


fields = std_fields
