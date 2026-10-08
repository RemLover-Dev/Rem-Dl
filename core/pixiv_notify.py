"""Pixiv following-feed watcher -> persistent notifications.

Feed-based only: one paginated call to the account's own Following
timeline (/v2/illust/follow), no per-artist polling. gallery-dl remains
the downloader; this only records what's new so the UI can notify.
"""

import threading
import time
from urllib.parse import unquote, parse_qs

import requests

from core import notifications
from core.database import DatabaseManager

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
    """Legacy-shaped view kept for callers/tests; storage lives in
    core.notifications, source-scoped to pixiv."""
    return {
        "items": notifications.items("pixiv"),
        "seen": notifications.seen_keys("pixiv"),
        "last_check": notifications.last_check("pixiv"),
    }


def load_items():
    return notifications.items("pixiv")


def _stamp_check(ts):
    STATE["last_check"] = ts
    notifications.stamp_check("pixiv", ts)


def last_check_ts():
    return notifications.last_check("pixiv")


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
    return notifications.known("pixiv")


def ingest(works):
    """works: raw illust dicts, newest first. Returns the new entries."""
    entries = []
    for w in works:
        e = entry_from_work(w)
        if e["work_id"]:
            e["external_id"] = e["work_id"]
            entries.append(e)
    return notifications.ingest("pixiv", entries)


def clear_items():
    """Drop all notifications but remember their ids, so the next scan
    doesn't re-notify the same works."""
    notifications.clear("pixiv")


def mark_read(ids=None):
    # dedup check + write span one lock inside notifications: an ingest()
    # landing in between must not be clobbered by a stale write-back
    return notifications.mark_read("pixiv", ids)


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
            _emit_fn({
                "source": "pixiv",
                "new": new_count,
                "unread": notifications.unread(),
                "unread_by_source": notifications.unread_by_source(),
            })
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
