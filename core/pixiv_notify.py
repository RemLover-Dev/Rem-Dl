"""Pixiv following-feed watcher -> persistent notifications.

Feed-based only: one paginated call to the account's own Following
timeline (/v2/illust/follow), no per-artist polling. gallery-dl remains
the downloader; this only records what's new so the UI can notify.
"""

import os
import threading
import time
from urllib.parse import unquote, parse_qs

import requests

from core.database import DatabaseManager, DATABASE_DIR

NOTIF_FILE = os.path.join(DATABASE_DIR, "pixiv_notifications.json")
MAX_ITEMS = 500
FOLLOW_ENDPOINT = "/v2/illust/follow"

STATE = {
    "watching": False,
    "checking": False,
    "last_check": None,
    "last_new": 0,
    "error": "",
    "pending_toast": 0,  # new works found but not yet toasted by the UI
}

_check_lock = threading.Lock()   # held for a whole check (may sit in rate-limit waits)
_state_lock = threading.Lock()   # guards pending_toast only — GET must never block
_file_lock = threading.Lock()    # read-modify-write on the store file
_wakeup = threading.Event()
_emit_fn = None
_get_config = lambda: {}
_log_fn = None
_hint_logged = False

TOKEN_HINT = [
    "[Pixiv] Refresh token missing or invalid — get a new one (same flow as gallery-dl oauth:pixiv):",
    "1. Open Settings → Pixiv and click 'Get login URL' (https://app-api.pixiv.net/web/v1/login?client=pixiv-android + a code_challenge)",
    "2. Log in; F12 → Network → select the last 'callback?state=...' entry",
    '3. Copy its "code" query value (the whole callback URL works too), paste it into the field, click "Get token"',
    "The code expires ~30 seconds after login — paste it quickly.",
]


def _log_token_hint():
    """Tell the user exactly how to get a token — once per app run."""
    global _hint_logged
    if _hint_logged or not _log_fn:
        return
    _hint_logged = True
    for line in TOKEN_HINT:
        try:
            _log_fn(line)
        except Exception:
            pass


def _load_raw():
    raw = DatabaseManager.load_json(NOTIF_FILE)
    if not isinstance(raw, dict):
        raw = {}
    raw.setdefault("items", [])
    raw.setdefault("seen", [])  # ids cleared from the list — never re-notify
    # persisted wall-clock stamp: the 1-2h timer keeps counting while the
    # app or PC is off; on open the watcher scans if the interval has elapsed
    raw.setdefault("last_check", 0)
    return raw


def _save_raw(raw):
    DatabaseManager.save_json(NOTIF_FILE, raw)


def load_items():
    with _file_lock:
        return _load_raw()["items"]


def save_items(items):
    with _file_lock:
        raw = _load_raw()
        raw["items"] = items
        _save_raw(raw)


def _stamp_check(ts):
    STATE["last_check"] = ts
    with _file_lock:
        raw = _load_raw()
        raw["last_check"] = ts
        _save_raw(raw)


def last_check_ts():
    with _file_lock:
        return _load_raw()["last_check"]


def seconds_until_due():
    interval = interval_minutes() * 60
    return max(0, interval - (time.time() - last_check_ts()))


def unread_count(items=None):
    if items is None:
        items = load_items()
    return sum(1 for i in items if not i.get("read"))


def entry_from_work(w):
    u = w.get("user") or {}
    return {
        "work_id": str(w.get("id") or ""),
        "title": w.get("title") or "",
        "artist_id": str(u.get("id") or ""),
        "artist_name": u.get("name") or "",
        "thumb": (w.get("image_urls") or {}).get("square_medium") or "",
        "create_date": w.get("create_date") or "",
        "discovered_at": time.time(),
        "read": False,
    }


def known_ids():
    with _file_lock:
        raw = _load_raw()
        return {i.get("work_id") for i in raw["items"]} | set(raw["seen"])


def ingest(works):
    """works: raw illust dicts, newest first. Returns the new entries."""
    with _file_lock:
        raw = _load_raw()
        known = {i.get("work_id") for i in raw["items"]} | set(raw["seen"])
        fresh = []
        for w in works:
            e = entry_from_work(w)
            if not e["work_id"] or e["work_id"] in known:
                continue
            known.add(e["work_id"])
            fresh.append(e)
        if fresh:
            # newest-first feed, newest-first store; bound size (ponytail: after
            # 500 newer works an old id can be re-discovered as "new" — harmless)
            raw["items"] = (fresh + raw["items"])[:MAX_ITEMS]
            _save_raw(raw)
        return fresh


def clear_items():
    """Drop all notifications but remember their ids, so the next scan
    doesn't re-notify the same works."""
    with _file_lock:
        raw = _load_raw()
        ids = [i.get("work_id") for i in raw["items"] if i.get("work_id")]
        # ponytail: seen only grows on clear, bounded at 2000 — an evicted id
        # could re-notify once, but only after 2000 newer works arrive first
        raw["seen"] = (ids + raw["seen"])[:2000]
        raw["items"] = []
        _save_raw(raw)


def mark_read(ids=None):
    # one lock span: load_items()+save_items() as two sections let an
    # ingest() landing in between be clobbered by this stale write-back
    with _file_lock:
        raw = _load_raw()
        items = raw["items"]
        for i in items:
            if ids is None or i.get("work_id") in ids:
                i["read"] = True
        _save_raw(raw)
    return unread_count(items)


def interval_minutes():
    """Watch cadence: 1-2 h (rate limits). Wall-clock, persisted."""
    try:
        v = int(DatabaseManager.load_ui_config().get("pixiv_watch_minutes", 90))
    except (TypeError, ValueError):
        v = 90
    return max(60, min(120, v))


def _build_api(cfg):
    from workers.pixiv import PixivAppAPI  # local: pulls PIL and friends
    session = requests.Session()
    if cfg.get("use_proxy") and cfg.get("proxy_url"):
        p = cfg["proxy_url"]
        session.proxies = {"http": p, "https": p}
    session.verify = cfg.get("verify_tls", False)
    log = lambda *a, **k: None  # noqa: E731 — watcher must not spam worker logs
    return PixivAppAPI(session, log, cfg.get("refresh_token") or "")


def _fetch_works(api, known):
    """One following feed walk: pages until a known work shows up (feed is
    newest-first) or the page cap is hit (caps the first-ever run)."""
    works = []
    params = {"restrict": "all"}
    for _ in range(3):
        data = api._call(FOLLOW_ENDPOINT, params)
        batch = data.get("illusts") or []
        works.extend(batch)
        if any(str(w.get("id")) in known for w in batch):
            break
        nxt = data.get("next_url")
        if not nxt:
            break
        qs = parse_qs(nxt.rpartition("?")[2])
        params = {k: unquote(v[0]) for k, v in qs.items()}
    return works


def run_check():
    """Fetch the feed and ingest. Never raises; returns {new, error, busy?}."""
    if not _check_lock.acquire(blocking=False):
        return {"new": 0, "error": STATE["error"], "busy": True}
    STATE["checking"] = True
    try:
        try:
            cfg = _get_config() or {}
            if not cfg.get("refresh_token"):
                STATE["error"] = "No Pixiv refresh token — set it in Settings → Pixiv"
                _log_token_hint()
                _stamp_check(time.time())
                return {"new": 0, "error": STATE["error"]}
            api = _build_api(cfg)
            fresh = ingest(_fetch_works(api, known_ids()))
            STATE["error"] = ""
            STATE["last_new"] = len(fresh)
            _stamp_check(time.time())
            if fresh:
                with _state_lock:
                    STATE["pending_toast"] += len(fresh)
            return {"new": len(fresh), "error": ""}
        except Exception as e:  # watcher failures must never take anything down
            STATE["error"] = str(e)[:300]
            _stamp_check(time.time())
            return {"new": 0, "error": STATE["error"]}
    finally:
        STATE["checking"] = False
        _check_lock.release()


def take_pending_toast():
    with _state_lock:
        n = STATE["pending_toast"]
        STATE["pending_toast"] = 0
    return n


def _notify(new_count):
    """Toast path: socket emit is authoritative; pending_toast only survives
    as the page-load fallback when the emit had no one to talk to."""
    delivered = False
    if new_count and _emit_fn:
        try:
            _emit_fn({"new": new_count, "unread": unread_count()})
            delivered = True
        except Exception:
            pass
    if delivered:
        with _state_lock:
            STATE["pending_toast"] = 0


def _watch_loop():
    time.sleep(8)  # let the server + frontend come up first
    while True:
        due = seconds_until_due()
        if due <= 0:
            res = run_check()
            if res.get("new"):
                _notify(res["new"])
            due = seconds_until_due()
        # sleep the remaining wall-clock interval (manual checks reset it)
        _wakeup.wait(timeout=max(1, due))
        _wakeup.clear()


def check_now():
    """Manual check from the UI. Returns False if one is already running."""
    if STATE["checking"]:
        return False

    def _run():
        res = run_check()
        if res.get("new"):
            _notify(res["new"])

    threading.Thread(target=_run, daemon=True).start()
    return True


def start_watcher(get_config, emit_fn, log_fn=None):
    global _get_config, _emit_fn, _log_fn
    _get_config = get_config
    _emit_fn = emit_fn
    _log_fn = log_fn
    if STATE["watching"]:
        return
    STATE["watching"] = True
    threading.Thread(target=_watch_loop, daemon=True).start()
