"""Gsbooru watcher adapter: /api/posts with Bearer key (GSBOORU_API_KEY
env; generate one in gsbooru account settings)."""

import os

from core.watchers.net import get

LIMIT = 100
API = "https://gsbooru.org/api/posts"
# rating is an integer {0: g, 1: s, 2: q, 3: e} (e empty live)
RATING_BY_INT = {0: "g", 1: "s", 2: "q", 3: "e"}


def query(tags, ratings):
    # tags param has no '-' exclusion (live-verified) — dashed tokens
    # become a local keep() filter; the rating param ORs comma lists and
    # all three codes == the whole site, same as no param
    tag = " ".join(str(t) for t in tags
                   if str(t).strip() and not str(t).startswith("-"))
    codes = sorted({str(r).split(":")[-1] for r in (ratings or [])
                    if str(r).split(":")[-1] in ("g", "s", "q")})
    return tag, (",".join(codes) if codes and len(codes) < 3 else "")


def keep(w, p):
    ex = {str(t)[1:] for t in (w.get("tags") or [])
          if str(t).startswith("-") and len(str(t)) > 1}
    if not ex:
        return True
    names = {t for t in str(p.get("tag_string") or "").split() if t}
    return not (ex & names)


def fetch(api_tag, pid):
    """One page (1-based; pid stays 0-based)."""
    tag, rating_param = api_tag
    key = os.getenv("GSBOORU_API_KEY", "")
    if not key:
        raise RuntimeError("gsbooru: GSBOORU_API_KEY missing in .env")
    params = {"tags": tag, "page": pid + 1, "limit": LIMIT}
    if rating_param:
        params["rating"] = rating_param
    resp = get(API, params=params, headers={"Authorization": f"Bearer {key}"})
    if resp.status_code in (401, 403, 429):
        raise RuntimeError(f"gsbooru HTTP {resp.status_code}")
    resp.raise_for_status()
    return (resp.json() or {}).get("posts") or []


def fields(p):
    r = p.get("rating")
    if isinstance(r, int):
        r = RATING_BY_INT.get(r, "")
    # the API exposes no preview — the full file doubles as the thumb
    return (p.get("file_url") or "", r or "", p.get("file_url") or "")
