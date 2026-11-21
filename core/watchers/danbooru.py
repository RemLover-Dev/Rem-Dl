"""Danbooru watcher adapter: posts.json. Rating metatags take a comma
list natively (rating:g,s) — no negation trick needed."""

from core.watchers.net import RATING_TOKENS, api_settings, get

API = "https://danbooru.donmai.us/posts.json"
LIMIT = 200
NAME = "Danbooru"


def query(tags, ratings):
    tag = " ".join(str(t) for t in tags if str(t).strip())
    codes = sorted({str(r).split(":")[-1] for r in (ratings or [])
                    if str(r).strip() in RATING_TOKENS})
    if codes and len(codes) < 4:
        tag = (tag + " rating:" + ",".join(codes)).strip()
    return tag


def fetch(api_tag, pid):
    """One danbooru page (page param is 1-based; pid stays 0-based)."""
    params = {"tags": api_tag, "page": pid + 1, "limit": LIMIT}
    cfg = api_settings()
    login = cfg.get("danbooru_login") or ""
    api_key = cfg.get("danbooru_api_key") or ""
    if login and api_key:
        params["login"] = login
        params["api_key"] = api_key
    resp = get(API, params=params)
    if resp.status_code in (401, 403, 429):
        raise RuntimeError(f"HTTP {resp.status_code}")
    resp.raise_for_status()
    posts = resp.json() or []
    return posts if isinstance(posts, list) else []


def fields(p):
    # danbooru names its preview field differently from the dapi sites
    return (p.get("preview_file_url") or p.get("large_file_url") or "",
            p.get("rating", ""), p.get("file_url") or "")
