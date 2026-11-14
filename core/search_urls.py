"""Search-URL builders for the Planning List's Open-on-Site button.

Every URL uses the same tag/rating conventions the site's downloader
worker (workers/) sends into its API, so opening a planned entry shows
the same result set the download would pull from:

- danbooru-style sites accept `rating:` metatags, comma-OR (verified in
  danbooru.py / gsbooru.py).
- gelbooru ANDs rating tags — a multi-rating becomes negation complements
  (workers/gelbooru.py build_query, reused verbatim here).
- moebooru sites (konachan / yande.re / sankaku) express only ONE rating.
- sites with no tag operator get a warning instead of a silently wrong
  URL (`-tag` tokens cannot alter the search there).
"""
import json
import os
import re
from urllib.parse import quote, quote_plus

from core.database import DATABASE_DIR

_BASE = {
    "dan": "https://danbooru.donmai.us/posts",
    "gelbooru": "https://gelbooru.com/index.php",
    "gsbooru": "https://gsbooru.org/posts",
    "kona": "https://konachan.com/post",
    "yande": "https://yande.re/post",
    "safe": "https://safebooru.org/index.php",
    "sankaku": "https://sankaku.app/",
    "rule34": "https://rule34.xxx/index.php",
    "zero": "https://www.zerochan.net",
    "anime_dl": "https://anime-pictures.net/posts",
    "pixiv": "https://www.pixiv.net",
    "pinterest": "https://www.pinterest.com/search/pins",
    "eshuushuu": "https://e-shuushuu.net/search",
    "neko": "https://nekos.best",
    "nekos_life": "https://nekos.life",
    "nekosia": "https://nekosia.net",
    "nekosapi": "https://nekosapi.com",
    "x": "https://x.com/search",
}

_RATING_TOKEN = re.compile(r"^rating:([gsqe])$", re.IGNORECASE)
_MODE_TOKEN = re.compile(r"^(artworks|search|bookmark|ranking):(.+)$", re.IGNORECASE)


def _rating_codes(rating):
    seen = []
    for part in str(rating or "").split():
        m = _RATING_TOKEN.match(part.strip().lower())
        if m and m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


def _split(tags):
    toks = str(tags or "").split()
    return [t for t in toks if not t.startswith("-")], \
           [t for t in toks if t.startswith("-")]


def _quote_q(tokens):
    return quote_plus(" ".join(t.strip() for t in tokens if t.strip()).strip())


def build_search_url(site, tags, rating="", timeout=15, esh_resolver=None):
    """Return (url|None, warnings) for one planning entry.

    url None means there was nothing sensible to open (failed tag-id
    resolution etc.) — the reason is then the first warning.
    esh_resolver: callable(word, timeout) -> id|"" — injectable for tests."""
    site = str(site or "").strip().lower()
    tags = str(tags or "").strip()
    warnings = []
    codes = _rating_codes(rating)
    if not tags:
        return (None, ["no tags to build a search for"])

    pos, neg = _split(tags)
    # metatag punctuation stays literal in the query — booru sites decode
    # percent-encoding, but '+', ':' and ',' are read literally by everyone
    enc = lambda s: quote_plus(s.strip(), safe=":,()!")

    if site in ("neko", "nekos_life", "nekosia", "nekosapi"):
        return (_BASE[site], ["this site has no tag-search page — opening the homepage"])

    if site == "pixiv":
        return (_pixiv_url(tags, rating, warnings), warnings)

    extra_qs = ""
    if site == "dan":
        part = " ".join(pos + neg)
        if codes and len(codes) < 4:
            part += " rating:" + ",".join(codes)
    elif site == "gelbooru":
        from workers.gelbooru import build_query
        part, _, _ = build_query(tags, rating)
        extra_qs = "page=post&s=list&"
    elif site == "gsbooru":
        for c in codes:
            if c == "e":
                warnings.append("gsbooru has no rating 'e' — dropped it from the metatag")
        part = " ".join(pos + neg)
        keep = [c for c in codes if c in "gsq"]
        if keep and len(keep) < 3:
            part += " rating:" + ",".join(keep)
    elif site in ("kona", "yande", "sankaku"):
        supported = set("sqe")
        for c in codes:
            if c not in supported:
                warnings.append(f"rating '{c}' is not supported by {site} — dropped it")
        part = " ".join(pos + neg)
        keep = [c for c in codes if c in supported]
        if len(keep) > 1:
            # moebooru has no comma-OR in the rating metatag — not applying a filter
            warnings.append(f"{site} search supports a single rating filter — the entry's multi-rating cannot be applied")
        elif keep:
            part += " rating:" + keep[0]
    elif site in ("safe", "rule34"):
        if codes:
            warnings.append(f"{site} has no rating filter in this app — ignoring the entry's rating")
        part = enc(" ".join(pos + neg))
        if site == "safe":
            return (f"{_BASE['safe']}?page=post&s=list&tags={part}", warnings)
        return (f"{_BASE['rule34']}?page=post&s=list&tags={part}", warnings)
    elif site == "zero":
        kept = [p.strip() for p in pos if p.strip()]
        if neg:
            warnings.append("zerochan has no '-' negation operator — the excluded tags were dropped")
        if not kept:
            return (_BASE["zero"], warnings)
        return (_BASE["zero"] + "/" + ",".join(quote_plus(p.strip(), safe="()!") for p in kept), warnings)
    elif site == "anime_dl":
        if neg:
            warnings.append("anime-pictures.net has no '-' negation operator — the excluded tags were dropped")
        return (f"{_BASE['anime_dl']}?page=0&search_tag={enc(' '.join(pos))}&order_by=date&lang=en", warnings)
    elif site == "pinterest":
        return (f"{_BASE['pinterest']}/?q={_quote_q(pos + neg)}", warnings)
    elif site == "x":
        return (f"{_BASE['x']}?q={_quote_q(pos + neg)}", warnings)
    elif site == "eshuushuu":
        return _eshuushuu_url(pos, neg, timeout, warnings, esh_resolver)
    else:
        return (None, [f"no search URL defined for site '{site}'"])

    return (
        f"{_BASE[site]}?{extra_qs}tags={enc(part)}",
        warnings,
    )


def _pixiv_url(tags, rating, warnings):
    if str(rating or "").strip():
        warnings.append("pixiv search URLs have no rating filter — the entry's rating was ignored")
    tokens = tags.strip().split()
    announced = [t for t in tokens if t.startswith("-")]
    if announced:
        warnings.append("pixiv search has no '-' negation (the query is a plain string) — excluded tokens were dropped")
    m = _MODE_TOKEN.match(tokens[0])
    if m:
        mode = m.group(1).lower()
        value = m.group(2).strip()
        extra = " ".join(t for t in tokens[1:]
                         if not _MODE_TOKEN.match(t) and not t.startswith("-"))
        if extra:
            value = (value + " " + extra).strip()
        if mode == "artworks" and value.isdigit():
            return f"{_BASE['pixiv']}/en/users/{value}"
        if mode == "bookmark" and value.isdigit():
            return f"{_BASE['pixiv']}/en/users/{value}/bookmarks/artworks"
        if mode == "ranking" and value:
            return f"{_BASE['pixiv']}/ranking.php?mode={quote(value)}"
        if mode == "artworks" and value:
            warnings.append("'artworks:' mode needs a pixiv user id — falling back to a tag search")
        if mode in ("artworks", "search") and value:
            return f"{_BASE['pixiv']}/en/tags/{quote_plus(value, safe='()!')}"
        return None
    words = [t for t in tokens if not (t.startswith("-") or _MODE_TOKEN.match(t))]
    if not words:
        return None
    return f"{_BASE['pixiv']}/en/tags/{quote_plus(' '.join(words), safe='()!')}"


def _eshuushuu_url(pos, neg, timeout, warnings, esh_resolver=None):
    if not pos:
        return (None, ["no resolvable tags for e-shuushuu"])
    if neg:
        warnings.append("e-shuushuu search has no '-' negation operator — the excluded tags were dropped")
    # single tag per query (verified: tags= text is silently ignored, only
    # numeric ids count) — resolve words in order, open the first hit
    resolver = esh_resolver or (lambda w, t: _esh_id_from_cache(w) or _esh_id_online(w, t))
    unseen = len(pos)
    for word in pos:
        tid = resolver(word, timeout)
        if tid:
            if unseen > 1:
                warnings.append(f"e-shuushuu search supports one tag per query — opened the URL for '{word}'")
            return (f"{_BASE['eshuushuu']}?tags={tid}", warnings)
    return (None, ["e-shuushuu tag ids could not be resolved for '"
                   + ", ".join(pos) + "' — local cache was empty and the tags API had no exact match"])


def _esh_id_from_cache(name):
    try:
        path = os.path.join(DATABASE_DIR, "eshuushuu_tags.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except OSError:
        return ""
    for entry in data or []:
        if isinstance(entry, dict) and str(entry.get("title", "")).lower() == str(name).lower():
            return str(entry.get("tag_id", ""))
    return ""


def _esh_id_online(name, timeout=15, _depth=0):
    try:
        import requests
        resp = requests.get("https://e-shuushuu.net/api/v1/tags",
                            params={"search": name}, timeout=timeout)
    except Exception:
        return ""
    try:
        if resp.status_code != 200:
            return ""
        payload = resp.json()
        tags = payload.get("tags", []) if isinstance(payload, dict) else []
        exact = [t for t in tags if isinstance(t, dict)
                 and str(t.get("title", "")).lower() == name.lower()]
        if not exact:
            return ""
        canon = [t for t in exact if not t.get("is_alias")]
        if canon:
            canon.sort(key=lambda t: t.get("usage_count") or 0, reverse=True)
            tid = canon[0].get("tag_id")
            return str(tid) if tid else ""
        if _depth == 0:
            target = exact[0].get("alias_of_name") or ""
            if target:
                return _esh_id_online(target, timeout, _depth + 1)
    except (ValueError, AttributeError, KeyError, TypeError):
        return ""
    return ""
