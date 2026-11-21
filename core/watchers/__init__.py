"""Configurable tag watchers -> shared notifications.

One centralized scheduler with a small bounded pool: each enabled, due
watcher runs check_watcher() -- a bounded, resumable newest-first scan of
a tag feed -- so one slow site cannot starve the rest. Every site is adapted behind its own module
(core/watchers/<site>.py exposing query/fetch/fields/LIMIT and an
optional keep(w, post) filter); the checkpoint/baseline engine here is
source-agnostic. Cursor-paged sites (X) expose fetch_pages(api_tag, w)
instead of fetch: it returns the whole new window in one call.

Checkpoint rules:
  * first check with results = baseline: only posts uploaded after the
    watcher was created/last reconfigured are notified, older ones are
    pre-existing noise (legacy watchers without baseline_at swallow all;
    sources without created_at swallow their baseline too);
  * last_seen_post_id only advances after a scan successfully reaches it
    (or exhausts the result set) -- a failed or capped check never skips
    posts silently;
  * a cap-hit keeps the checkpoint untouched, records an error, and
    resumes deeper via scan_pid on the next check.

Test seam: each adapter's fetch() is monkeypatchable directly
(monkeypatch.setattr(watchers.<site>, "fetch", fake)).
"""

import datetime
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from core import notifications
from core.database import DATABASE_DIR, DatabaseManager, get_settings

WATCHERS_FILE = os.path.join(DATABASE_DIR, "watchers.json")
MAX_PAGES_PER_CHECK = 10   # bounded scan per check (resumable via scan_pid)
# ponytail: unreachable checkpoint after 100 pages -> re-baseline instead
# of paging through a site's whole history forever
MAX_CYCLE_PAGES = 100
ANTI_BAN_PAUSE = float(os.getenv("ANTI_BAN_PAUSE", "3.0"))

from core.watchers.net import RATING_TOKENS  # noqa: E402

from core.watchers import (  # noqa: E402
    anime_dl,
    danbooru,
    eshuushuu,
    gelbooru,
    gsbooru,
    konachan,
    pixiv,
    rule34,
    sankaku,
    safebooru,
    x_twitter,
    yande,
    zerochan,
)

_ADAPTERS = {
    "gelbooru": gelbooru,
    "danbooru": danbooru,
    "safebooru": safebooru,
    "rule34": rule34,
    "konachan": konachan,
    "yande": yande,
    "sankaku": sankaku,
    "gsbooru": gsbooru,
    "zerochan": zerochan,
    "eshuushuu": eshuushuu,
    "anime_dl": anime_dl,
    "pixiv": pixiv,
    "x": x_twitter,
}
SOURCES = tuple(_ADAPTERS)

_lock = threading.RLock()
_wakeup = threading.Event()
_STATE = {"running": False}
_emit_fn = None
_log_fn = None
_INFLIGHT = set()
_INFLIGHT_LOCK = threading.Lock()
_POOL = None


# ---------- store ----------

def _load():
    with _lock:
        raw = DatabaseManager.load_json(WATCHERS_FILE)
        if not isinstance(raw, dict):
            raw = {}
        raw.setdefault("watchers", [])
        return raw


def _save(raw):
    DatabaseManager.save_json(WATCHERS_FILE, raw)


def list_watchers(source=None):
    with _lock:
        ws = _load()["watchers"]
        return [dict(w) for w in ws if source is None or w.get("source") == source]


def _set_runtime(wid, fields, snapshot=None):
    """Persist checkpoint/scan/status fields without racing CRUD.

    snapshot: the watcher copy check_watcher ran with. When a CRUD update
    re-baselined the watcher mid-check (tags/ratings changed -> stored
    initialized dumped back to False), the snapshot's checkpoint/cursor
    verdicts belong to the OLD query — writing them would resurrect the old
    cursor and re-flood old posts under the new tags. The write is skipped
    instead; the next cycle runs on the fresh baseline."""
    with _lock:
        raw = _load()
        for w in raw["watchers"]:
            if w["watcher_id"] == wid:
                if (snapshot is not None
                        and snapshot.get("initialized")
                        and not w.get("initialized")):
                    return None
                w.update(fields)
                _save(raw)
                return dict(w)
    return None


def _clean(data):
    """Validate + normalize patch fields. Returns (cleaned, error)."""
    out = {}
    if "tags" in data:
        tags = [str(t).strip().lower() for t in (data.get("tags") or [])
                if str(t).strip()]
        if not tags:
            return None, "at least one tag is required"
        out["tags"] = tags
    if "ratings" in data:
        ratings = [str(r).strip() for r in (data.get("ratings") or [])
                   if str(r).strip()]
        bad = [r for r in ratings if r not in RATING_TOKENS]
        if bad:
            return None, f"unknown rating: {bad[0]}"
        out["ratings"] = ratings
    if "interval_minutes" in data:
        try:
            iv = int(data["interval_minutes"])
        except (TypeError, ValueError):
            return None, "interval must be a number of minutes"
        out["interval_minutes"] = max(5, min(1440, iv))
    if "enabled" in data:
        out["enabled"] = bool(data["enabled"])
    return out, None


def create(data):
    source = str(data.get("source") or "")
    if source not in SOURCES:
        return None, f"unsupported source: {source or '?'}"
    fields, err = _clean(data)
    if err:
        return None, err
    if not fields.get("tags"):
        return None, "at least one tag is required"
    w = {
        "watcher_id": "w_" + uuid.uuid4().hex[:8],
        "source": source,
        "tags": fields["tags"],
        "ratings": fields.get("ratings") or [],
        "interval_minutes": fields.get("interval_minutes") or 5,
        "enabled": True,
        "initialized": False,
        "baseline_at": time.time(),
        "last_seen_post_id": None,
        "last_checked_at": 0,
        "scan_pid": 0,
        "scan_newest_id": None,
        "error": "",
    }
    with _lock:
        raw = _load()
        raw["watchers"].append(w)
        _save(raw)
    _wakeup.set()
    return dict(w), None


def update(wid, data):
    fields, err = _clean(data)
    if err:
        return None, err
    with _lock:
        raw = _load()
        w = next((x for x in raw["watchers"] if x["watcher_id"] == wid), None)
        if w is None:
            return None, "watcher not found"
        config_changed = any(k in fields and fields[k] != w.get(k)
                             for k in ("tags", "ratings"))
        w.update(fields)
        if config_changed:
            # a different result set needs a fresh baseline: no flood of
            # old posts, no stale checkpoint from the old query
            w.update({"initialized": False, "last_seen_post_id": None,
                      "baseline_at": time.time(),
                      "scan_pid": 0, "scan_newest_id": None, "error": ""})
        _save(raw)
        out = dict(w)
    _wakeup.set()
    return out, None


def remove(wid):
    with _lock:
        raw = _load()
        keep = [w for w in raw["watchers"] if w["watcher_id"] != wid]
        if len(keep) == len(raw["watchers"]):
            return False
        raw["watchers"] = keep
        _save(raw)
    _wakeup.set()
    return True




# ---------- shared check engine ----------

def _source(src):
    """Per-site adapter module. Unknown source falls back to gelbooru's."""
    return _ADAPTERS.get(src) or _ADAPTERS["gelbooru"]


def _ts_flex(v):
    """created_at in any site's format -> epoch (0 if unparseable)."""
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v)
    try:
        return datetime.datetime.fromisoformat(
            s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        pass
    try:
        return datetime.datetime.strptime(
            s, "%a %b %d %H:%M:%S %z %Y").timestamp()
    except ValueError:
        return 0.0


def _collect(posts, checkpoint, fresh, keep):
    """Append until the checkpoint id appears (feed is newest-first)."""
    for p in posts:
        if not isinstance(p, dict):
            continue
        if str(p.get("id")) == checkpoint:
            return fresh, True
        if keep and not keep(p):
            continue
        fresh.append(p)
    return fresh, False


def _entries(w, posts):
    title = " \u2022 ".join(w.get("tags") or [])[:120]
    fields = _source(w.get("source")).fields
    out = []
    for p in posts:
        thumb, rating, url = fields(p)
        out.append({
            "external_id": str(p.get("id")),
            "watcher_id": w["watcher_id"],
            "created_at": time.time(),
            "read": False,
            "title": title,
            # cheap thumb: the site's own preview, never the full-res file
            "thumb": thumb,
            "rating": rating,
            "metadata": {"tags": w.get("tags") or [],
                         "ratings": w.get("ratings") or [],
                         "post_id": str(p.get("id")),
                         "url": url},
        })
    return out


def check_watcher(w):
    """One watcher check. Never raises; returns {new, error}."""
    wid = w["watcher_id"]
    src = w.get("source")
    try:
        mod = _source(src)
        api_tag = mod.query(w.get("tags") or [], w.get("ratings") or [])
        keep_fn = getattr(mod, "keep", None)
        # adapter contract is keep(watcher, post) -- it may read the
        # watcher's tags/ratings, so bind the watcher here
        keep = (lambda p: keep_fn(w, p)) if keep_fn else None
        limit = mod.LIMIT
        pid = int(w.get("scan_pid") or 0)
        checkpoint = w.get("last_seen_post_id")
        cycle_newest = w.get("scan_newest_id")
        fresh = []
        reached = exhausted = False
        pages_done = 0

        if pid == 0:
            fetch_pages = getattr(mod, "fetch_pages", None)
            if fetch_pages:
                # cursor-paged sites (X): the adapter returns the whole new
                # window at once, so there is no page cursor to resume
                posts = fetch_pages(api_tag, w)
                pages_done = MAX_PAGES_PER_CHECK
            else:
                posts = mod.fetch(api_tag, 0)
                pages_done = 1
            if not posts:
                _set_runtime(wid, {"last_checked_at": time.time(), "error": ""})
                return {"new": 0, "error": ""}
            if not w.get("initialized"):
                # baseline: only notify posts uploaded after baseline_at
                # (creation / last reconfig); older ones are pre-existing
                # noise. Legacy watchers without baseline_at = unknown
                # creation time -> conservative swallow-all.
                # ponytail: window = newest page only; a busier
                # tag offline longer loses the tail -- paginate the
                # baseline if that ever bites
                cutoff = float(w.get("baseline_at") or 0)
                fresh = [p for p in posts
                         if cutoff > 0 and _ts_flex(p.get("created_at")) >= cutoff
                         and (not keep or keep(p))]
                new_entries = (notifications.ingest(w["source"],
                                                    _entries(w, fresh))
                               if fresh else [])
                _set_runtime(wid, {"initialized": True,
                                   "last_seen_post_id": str(posts[0]["id"]),
                                   "last_checked_at": time.time(), "error": ""},
                             snapshot=w)
                return {"new": len(new_entries), "error": ""}
            cycle_newest = str(posts[0]["id"])
            if fetch_pages:
                # the adapter already bounded the window (new posts only)
                fresh = [p for p in posts if not keep or keep(p)]
                reached = True
            else:
                fresh, reached = _collect(posts, checkpoint, fresh, keep)
            pid = 1

        while not reached and not exhausted and pages_done < MAX_PAGES_PER_CHECK:
            time.sleep(ANTI_BAN_PAUSE)
            posts = mod.fetch(api_tag, pid)
            pages_done += 1
            if not posts:
                exhausted = True
                break
            pid += 1
            fresh, reached = _collect(posts, checkpoint, fresh, keep)

        if reached or exhausted:
            # only here, after successful fetches, does the checkpoint move
            new_entries = notifications.ingest(w["source"], _entries(w, fresh))
            _set_runtime(wid, {"last_seen_post_id": cycle_newest or checkpoint,
                               "initialized": True, "scan_pid": 0,
                               "scan_newest_id": None,
                               "last_checked_at": time.time(), "error": ""},
                         snapshot=w)
            return {"new": len(new_entries), "error": ""}

        # cap hit: collected posts are notified (dedup makes re-notify
        # impossible); checkpoint stays put so nothing below is skipped
        new_entries = notifications.ingest(w["source"], _entries(w, fresh))
        if pid > MAX_CYCLE_PAGES:
            err = (f"checkpoint unreachable after {pid * limit} posts "
                   "- re-baselined")
            _set_runtime(wid, {"last_seen_post_id": cycle_newest or checkpoint,
                               "initialized": True, "scan_pid": 0,
                               "scan_newest_id": None,
                               "last_checked_at": time.time(), "error": err},
                         snapshot=w)
            return {"new": len(new_entries), "error": err}
        err = f"Too many new posts; watcher capped at {MAX_PAGES_PER_CHECK * limit} results"
        # one write per check: scan_newest_id rides along so a resumed
        # cycle still knows the top of the feed it is paging toward
        _set_runtime(wid, {"scan_pid": pid, "scan_newest_id": cycle_newest,
                           "last_checked_at": time.time(), "error": err},
                     snapshot=w)
        return {"new": len(new_entries), "error": err}
    except Exception as e:  # watcher failures must never take anything down
        # name the site with its display capitalization ("Danbooru: HTTP 403")
        name = getattr(mod, "NAME", str(src).title())
        err = str(e)
        if not err.lower().startswith(name.lower()):
            err = f"{name}: {err}"
        err = err[:300]
        _set_runtime(wid, {"last_checked_at": time.time(), "error": err})
        return {"new": 0, "error": err}


# ---------- scheduler ----------

_manual_lock = threading.Lock()  # one manual run at a time


def check_now(source=None):
    """Manual 'Check now' from the UI: run the source's enabled watchers
    right away, bypassing due timers (each stamps its own
    last_checked_at, so the interval resets like a scheduled check).
    Returns False if a manual run is already going."""
    if not _manual_lock.acquire(blocking=False):
        return False

    def _run():
        try:
            for w in list_watchers(source):
                if not w.get("enabled"):
                    continue
                res = check_watcher(w)
                if res.get("error"):
                    _log(f"[{w['source']}] {res['error']}")
                if res.get("new"):
                    _emit(w, res["new"])
        finally:
            _manual_lock.release()

    threading.Thread(target=_run, daemon=True).start()
    return True


def _due_at(w):
    return (float(w.get("last_checked_at") or 0)
            + float(w.get("interval_minutes") or 5) * 60)


def due_watchers(now=None):
    """Enabled watchers whose interval has elapsed. Pure — testable."""
    now = time.time() if now is None else now
    return [w for w in list_watchers()
            if w.get("enabled") and now >= _due_at(w)]


def _in_flight(wid):
    with _INFLIGHT_LOCK:
        return wid in _INFLIGHT


def _idle_due(now=None):
    """Due watchers that are not already being checked."""
    return [w for w in due_watchers(now) if not _in_flight(w.get("id"))]


def _seconds_until_due():
    enabled = [w for w in list_watchers() if w.get("enabled")]
    if not enabled:
        return 60.0
    pending = [w for w in enabled if not _in_flight(w.get("id"))]
    if not pending:
        return 5.0  # everything due is running; a done-callback wakes us
    return max(1.0, min(_due_at(w) for w in pending) - time.time())


def _log(msg):
    if _log_fn:
        try:
            _log_fn(msg)
        except Exception:
            pass


def _emit(w, new_count):
    if not _emit_fn:
        return
    try:
        _, unread, by = notifications.summary()
        _emit_fn({"source": w["source"], "new": new_count,
                  "unread": unread, "unread_by_source": by})
    except Exception:
        pass


def _submit(w):
    """Run one check on the pool; never two checks for the same watcher."""
    wid = w.get("id")
    with _INFLIGHT_LOCK:
        if wid in _INFLIGHT:
            return
        _INFLIGHT.add(wid)

    def run():
        try:
            prev_error = w.get("error") or ""
            res = check_watcher(w)
            if res.get("error") and res["error"] != prev_error:
                _log(f"[{w['source']}] {res['error']}")
            if res.get("new"):
                _emit(w, res["new"])
        finally:
            with _INFLIGHT_LOCK:
                _INFLIGHT.discard(wid)
            _wakeup.set()

    _POOL.submit(run)


def _loop():
    time.sleep(8)  # let the server + frontend come up first
    while True:
        for w in _idle_due():
            _submit(w)
        _wakeup.wait(timeout=_seconds_until_due())
        _wakeup.clear()


def _pool_size():
    """Watcher concurrency follows the user's Settings request limit."""
    try:
        return max(1, int(float(get_settings().get("req_rate_limit", 4))))
    except (TypeError, ValueError):
        return 4


def start_scheduler(emit_fn, log_fn=None):
    global _emit_fn, _log_fn, _POOL
    _emit_fn = emit_fn
    _log_fn = log_fn
    if _STATE["running"]:
        return
    _STATE["running"] = True
    _POOL = ThreadPoolExecutor(max_workers=_pool_size(), thread_name_prefix="rems-watcher")
    threading.Thread(target=_loop, daemon=True).start()
