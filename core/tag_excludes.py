"""Per-site permanent tag excludes applied to every download search.

Stored as one JSON map (site -> [tag]) and applied to every download search
on that site, so it keeps excluding them until removed here. Most sites get
``-tag`` tokens appended to the query; sites whose tags contain spaces pass
the exclusions to the worker separately.
"""
import os
import threading

from core.database import DATABASE_DIR, DatabaseManager

EXCLUDES_FILE = os.path.join(DATABASE_DIR, "tag_excludes.json")
_LOCK = threading.RLock()

SITES = ("anime_dl", "dan", "eshuushuu", "gelbooru", "gsbooru", "kona",
         "nekosapi", "nekosia", "rule34", "safe", "sankaku", "yande", "zero")

# Tags that contain spaces stay one token; zerochan also keeps its case.
_SPACE_TAGS = {"anime_dl", "eshuushuu", "nekosapi", "zero"}
# These sites separate tags with commas instead of spaces.
_COMMA_SITES = {"nekosapi", "zero"}

# Danbooru's free tier caps a search at 3 tags (rating excluded); reserving
# one slot for the user's own tag leaves at most 2 permanent exclusions.
MAX_PER_SITE = {"dan": 2}
DEFAULT_MAX = 20


def _site(site):
    return str(site or "").strip().lower()


def _cap(site):
    return MAX_PER_SITE.get(_site(site), DEFAULT_MAX)


def _clean(site, tags):
    out = []
    for tag in tags or []:
        raw = str(tag).strip().lstrip("-")
        if site == "zero":
            tag = " ".join(raw.split())
        elif site in _SPACE_TAGS:
            tag = " ".join(raw.lower().split())
        else:
            tag = "_".join(raw.lower().split())
        if tag and tag not in out:
            out.append(tag)
    return out


def _load():
    with _LOCK:
        raw = DatabaseManager.load_json(EXCLUDES_FILE)
        return raw if isinstance(raw, dict) else {}


def get(site):
    site = _site(site)
    tags = _load().get(site, [])
    return _clean(site, tags)[:_cap(site)]


def all():
    raw = _load()
    return {site: _clean(site, raw.get(site, []))[:_cap(site)] for site in SITES}


def save(site, tags):
    site = _site(site)
    if site not in SITES:
        return None, f"unknown site '{site}'"
    tags = _clean(site, tags)
    cap = _cap(site)
    if len(tags) > cap:
        return None, f"{site} allows at most {cap} excluded tags"
    with _LOCK:
        raw = _load()
        raw[site] = tags
        DatabaseManager.save_json(EXCLUDES_FILE, raw)
    return tags, ""


def apply(site, tag):
    """Append this site's permanent excludes to a download's search tag."""
    site = _site(site)
    tag = str(tag or "").strip()
    if site not in SITES or not tag:
        return tag
    sep = "," if site in _COMMA_SITES else " "
    parts = tag.split(sep)
    already = {t.strip().lstrip("-").lower() for t in parts
               if t.strip().startswith("-")}
    extra = [t for t in get(site) if t.lower() not in already]
    return (tag + sep + sep.join("-" + t for t in extra)).strip() if extra else tag
