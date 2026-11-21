"""Pixiv tag watcher: /v1/search/illust newest-first, same app-API token
as the follows notifier. Works carry create_date, so a fresh watcher
notifies posts made after it was created."""

import os

LIMIT = 30  # app-API search page size (offset = pid * LIMIT)
API = "/v1/search/illust"
NAME = "Pixiv"

# booru rating letter -> pixiv x_restrict (0 all-ages, 1 R-18, 2 R-18G);
# NSFW covers R18G because the shared rating box has no separate code
X_BY_CODE = {"g": {0}, "s": {0}, "q": {1}, "e": {1, 2}}

_api = None
_api_token = None


def query(tags, ratings):
    # pixiv search is a plain AND of words; '-' has no meaning there
    words = [str(t).strip() for t in tags]
    return " ".join(t for t in words if t and not t.startswith("-"))


def _get_api():
    global _api, _api_token
    from core.database import get_settings
    from core.pixiv_notify import _build_api
    st = get_settings()
    cfg = {
        "use_proxy": st.get("use_proxy"),
        "proxy_url": st.get("proxy_url"),
        "verify_tls": st.get("verify_tls"),
        "refresh_token": (st.load_api_settings().get("pixiv_refresh_token")
                          or os.getenv("PIXIV_REFRESH_TOKEN", "")),
    }
    if not cfg["refresh_token"]:
        raise RuntimeError("set a refresh token in Settings → Pixiv")
    if _api is None or _api_token != cfg["refresh_token"]:
        _api = _build_api(cfg)
        _api_token = cfg["refresh_token"]
    return _api


def fetch(api_tag, pid):
    if not api_tag:
        raise RuntimeError("enter at least one tag")
    data = _get_api()._call(API, {
        "word": api_tag,
        "sort": "date_desc",
        "search_target": "partial_match_for_tags",
        "offset": pid * LIMIT,
        "limit": LIMIT,
    })
    posts = data.get("illusts") or []
    # the engine's baseline cutoff reads created_at, pixiv calls it create_date
    for p in posts:
        p["created_at"] = p.get("create_date") or ""
    return posts


def keep(w, p):
    codes = {str(r).split(":")[-1].lower() for r in (w.get("ratings") or [])}
    if codes:
        allowed = set().union(*(X_BY_CODE.get(c, {0}) for c in codes))
        if int(p.get("x_restrict") or 0) not in allowed:
            return False
    ex = {str(t)[1:].lower() for t in (w.get("tags") or [])
          if str(t).startswith("-") and len(str(t)) > 1}
    if ex:
        names = {str(t.get("name") if isinstance(t, dict) else t).lower()
                 for t in (p.get("tags") or [])}
        if ex & names:
            return False
    return True


def fields(p):
    urls = p.get("image_urls") or {}
    meta = p.get("meta_single_page") or {}
    thumb = urls.get("square_medium") or urls.get("medium") or ""
    url = (meta.get("original_image_url") or urls.get("large")
           or urls.get("medium") or "")
    return thumb, "", url
