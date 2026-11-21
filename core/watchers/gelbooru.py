"""Gelbooru watcher adapter: dapi json."""

from core.watchers.net import api_settings, get, std_fields
from workers.gelbooru import build_query

API = "https://gelbooru.com/index.php"
LIMIT = 100
NAME = "Gelbooru"


def query(tags, ratings):
    # dapi has no comma-OR for ratings; the negation trick ORs them
    return build_query(" ".join(str(t) for t in tags if str(t).strip()),
                       " ".join(str(r) for r in (ratings or [])))[0]


def fetch(api_tag, pid):
    """One gelbooru page (0-based pid)."""
    params = {"page": "dapi", "s": "post", "q": "index", "tags": api_tag,
              "pid": pid, "limit": LIMIT, "json": 1}
    cfg = api_settings()
    api_key = cfg.get("gelbooru_api_key") or ""
    user_id = cfg.get("gelbooru_user_id") or ""
    if api_key and user_id:
        params["api_key"] = api_key
        params["user_id"] = user_id
    resp = get(API, params=params)
    if resp.status_code in (401, 403, 429):
        raise RuntimeError(f"HTTP {resp.status_code}")
    resp.raise_for_status()
    posts = (resp.json() or {}).get("post") or []
    # gelbooru returns a bare dict for a single-result page
    return posts if isinstance(posts, list) else [posts]


fields = std_fields
