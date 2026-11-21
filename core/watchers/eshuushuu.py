"""E-shuushuu watcher adapter: numeric tag ids resolved from the cached
tag table (written by the eshuushuu worker), then the site's tag API;
posts come from the search-page thumbs regex. No created_at in the
listing -> the baseline swallows everything (conservative)."""

import json
import os
import re

from core.database import DATABASE_DIR
from core.watchers.net import get

# search pages show ~20 thumbs — approximate, only feeds the cap text
LIMIT = 20
TAGS_DB = os.path.join(DATABASE_DIR, "eshuushuu_tags.json")
NAME = "E-shuushuu"


def _tag_id(name):
    """Cached tag id, then a live API lookup (exact title, one alias
    hop). Returns '' when unknown."""
    if os.path.exists(TAGS_DB):
        try:
            with open(TAGS_DB, encoding="utf-8") as f:
                for t in json.load(f):
                    if str(t.get("title", "")).lower() == name.lower():
                        return str(t.get("tag_id") or "")
        except Exception:
            pass
    try:
        resp = get("https://e-shuushuu.net/api/v1/tags",
                   params={"search": name}, timeout=15)
        if resp.status_code != 200:
            return ""
        tags = (resp.json() or {}).get("tags") or []
        exact = [t for t in tags if isinstance(t, dict)
                 and str(t.get("title", "")).lower() == name.lower()]
        canon = [t for t in exact if not t.get("is_alias")]
        if canon:
            canon.sort(key=lambda t: t.get("usage_count") or 0, reverse=True)
            return str(canon[0].get("tag_id") or "")
        target = (exact[0].get("alias_of_name") if exact else "") or ""
        if target:
            r2 = get("https://e-shuushuu.net/api/v1/tags",
                     params={"search": target}, timeout=15)
            if r2.status_code == 200:
                for t in (r2.json() or {}).get("tags") or []:
                    if (isinstance(t, dict)
                            and str(t.get("title", "")).lower() == target.lower()
                            and not t.get("is_alias")):
                        return str(t.get("tag_id") or "")
    except Exception:
        pass
    return ""


def query(tags, ratings):
    # the site search takes comma-joined numeric tag ids and has no
    # negation (live: tags=-ID still serves the plain feed) — dashed
    # tokens are dropped
    ids = []
    for t in tags:
        t = str(t).strip()
        if not t or t.startswith("-"):
            continue
        name = " ".join(t.replace("_", " ").split())
        tid = _tag_id(name)
        if not tid:
            raise RuntimeError(f"unknown tag '{t}'")
        ids.append(tid)
    return ",".join(ids)


def fetch(api_tag, pid):
    """One search page (1-based; pid stays 0-based). Posts are thumb
    paths in feed order; fields() prefixes the host later."""
    params = []
    if api_tag:
        params.append(f"tags={api_tag}")
    params.append(f"page={pid + 1}")
    resp = get(f"https://e-shuushuu.net/search?{'&'.join(params)}", timeout=15)
    if resp.status_code in (403, 429):
        raise RuntimeError(f"HTTP {resp.status_code}")
    resp.raise_for_status()
    if "/search" not in str(resp.url):
        return []  # unknown tag -> homepage redirect
    out, seen = [], set()
    for m in re.finditer(r'/thumbs/\d{4}-\d{2}-\d{2}-(\d+)\.webp', resp.text):
        if m.group(0) in seen:
            continue
        seen.add(m.group(0))
        out.append({"id": int(m.group(1)), "thumb": m.group(0)})
    return out


def fields(p):
    return (f"https://e-shuushuu.net{p.get('thumb') or ''}", "", "")
