"""Shared multi-source notification store.

One file for every source (pixiv, gelbooru, future workers): flat items
carrying {source, external_id, watcher_id, key, read, created_at, ...}
plus a seen-key set that stops re-notification after clear/eviction.

Dedup key is "{source}:{watcher_id}:{external_id}" — the same post may
legitimately match two independent watchers, each gets its own
notification and read state. The legacy single-source
pixiv_notifications.json is folded in once on first access.
"""

import os
import threading

from core.database import DatabaseManager, DATABASE_DIR

NOTIF_FILE = os.path.join(DATABASE_DIR, "notifications.json")
LEGACY_PIXIV_FILE = os.path.join(DATABASE_DIR, "pixiv_notifications.json")
MAX_ITEMS = 1000
# ponytail: seen only grows on clear, bounded — an evicted key could
# re-notify once, but only after MAX_ITEMS newer works arrive first
MAX_SEEN = 4000

_lock = threading.RLock()
_migration_done = False


def _key(source, external_id, watcher_id=""):
    return f"{source}:{watcher_id}:{external_id}"


def _save(raw):
    DatabaseManager.save_json(NOTIF_FILE, raw)


def _load():
    """Read the store; fold the legacy pixiv file in on first access."""
    global _migration_done
    with _lock:
        raw = DatabaseManager.load_json(NOTIF_FILE)
        if not isinstance(raw, dict):
            raw = {}
        raw.setdefault("items", [])
        raw.setdefault("seen", [])
        lc = raw.get("last_check")
        raw["last_check"] = lc if isinstance(lc, dict) else {}
        if not _migration_done:
            _migration_done = True
            _migrate_legacy_pixiv(raw)
        return raw


def _migrate_legacy_pixiv(raw):
    """Import the pre-multi-source pixiv file, then step aside (.migrated)."""
    if not os.path.exists(LEGACY_PIXIV_FILE):
        return
    legacy = DatabaseManager.load_json(LEGACY_PIXIV_FILE)
    if isinstance(legacy, dict):
        known = {i.get("key") for i in raw["items"]} | set(raw["seen"])
        fresh = []
        for it in legacy.get("items") or []:
            if not isinstance(it, dict):
                continue
            wid = str(it.get("work_id") or "")
            if not wid:
                continue
            e = dict(it)
            e["source"] = "pixiv"
            e["external_id"] = wid
            e["watcher_id"] = ""
            e["key"] = _key("pixiv", wid)
            if e["key"] in known:
                continue
            known.add(e["key"])
            fresh.append(e)
        seen = [_key("pixiv", s) for s in (legacy.get("seen") or [])
                if s and _key("pixiv", s) not in known]
        if fresh:
            raw["items"] = (fresh + raw["items"])[:MAX_ITEMS]
        if seen:
            raw["seen"] = (seen + raw["seen"])[:MAX_SEEN]
        lc = legacy.get("last_check")
        if isinstance(lc, (int, float)) and lc and "pixiv" not in raw["last_check"]:
            raw["last_check"]["pixiv"] = lc
        _save(raw)
    try:
        os.replace(LEGACY_PIXIV_FILE, LEGACY_PIXIV_FILE + ".migrated")
    except OSError:
        pass  # next start retries the import


def ingest(source, entries):
    """Add unseen entries (newest first). Each needs external_id; optional
    watcher_id scopes the dedup key. Dedup check + write run under one
    lock so a concurrent ingest/mark_read can never interleave.
    Returns the fresh entries with source/key set."""
    with _lock:
        raw = _load()
        known = {i.get("key") for i in raw["items"]} | set(raw["seen"])
        fresh = []
        for entry in entries:
            eid = str(entry.get("external_id") or "")
            if not eid:
                continue
            k = _key(source, eid, entry.get("watcher_id") or "")
            if k in known:
                continue
            known.add(k)
            e = dict(entry)
            e["source"] = source
            e["external_id"] = eid
            e["key"] = k
            fresh.append(e)
        if fresh:
            # newest-first store; bound size (ponytail: after MAX_ITEMS
            # newer works an old key can re-notify — harmless)
            raw["items"] = (fresh + raw["items"])[:MAX_ITEMS]
            _save(raw)
        return fresh


def items(source=None):
    with _lock:
        raw = _load()
        if source is None:
            return list(raw["items"])
        return [i for i in raw["items"] if i.get("source") == source]


def seen_keys(source=None):
    with _lock:
        raw = _load()
        if source is None:
            return list(raw["seen"])
        prefix = source + ":"
        return [k for k in raw["seen"] if k.startswith(prefix)]


def known(source):
    """external_ids already seen by a source (items + cleared/remembered)."""
    with _lock:
        raw = _load()
        ids = {i.get("external_id") for i in raw["items"]
               if i.get("source") == source}
        prefix = source + ":"
        for k in raw["seen"]:
            if k.startswith(prefix):
                ids.add(k.split(":", 2)[2])
        return ids


def unread(source=None):
    return sum(1 for i in items(source) if not i.get("read"))


def unread_by_source():
    counts = {}
    for i in items():
        if not i.get("read"):
            s = i.get("source") or "?"
            counts[s] = counts.get(s, 0) + 1
    return counts


def summary(source=None):
    """One load + one pass for API reads: filtered items, global unread,
    per-source unread counts. items()+unread()+unread_by_source() on the
    same request would re-parse the whole store three times."""
    with _lock:
        raw = _load()
        out, unread, by = [], 0, {}
        for i in raw["items"]:
            if not i.get("read"):
                unread += 1
                s = i.get("source") or "?"
                by[s] = by.get(s, 0) + 1
            if source is None or i.get("source") == source:
                out.append(i)
        return out, unread, by


def mark_read(source, keys=None):
    """keys=None → all of that source; otherwise matches item key or bare
    external_id (legacy callers pass work ids). Returns GLOBAL unread."""
    with _lock:
        raw = _load()
        kset = set(keys) if keys else None
        for i in raw["items"]:
            if i.get("source") != source:
                continue
            if (kset is None or i.get("key") in kset
                    or i.get("external_id") in kset):
                i["read"] = True
        _save(raw)
    return unread()


def clear(source):
    """Drop a source's notifications but remember their keys, so the next
    scan doesn't re-notify them."""
    with _lock:
        raw = _load()
        keep, moved = [], []
        for i in raw["items"]:
            if i.get("source") == source:
                if i.get("key"):
                    moved.append(i["key"])
            else:
                keep.append(i)
        raw["items"] = keep
        raw["seen"] = (moved + raw["seen"])[:MAX_SEEN]
        _save(raw)


def last_check(source):
    with _lock:
        return _load()["last_check"].get(source, 0)


def stamp_check(source, ts):
    """Source-level status stamp (pixiv's due-timer). NOT a watcher
    checkpoint — watchers own last_seen_post_id in watchers.json."""
    with _lock:
        raw = _load()
        raw["last_check"][source] = ts
        _save(raw)
