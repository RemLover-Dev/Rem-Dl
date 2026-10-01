import json
import os
import time

import pytest

# same env snapshot/restore dance as test_pixiv_notifications
_ENV_KEYS = [
    "USE_PROXY", "PROXY_URL", "VERIFY_TLS", "GELBOORU_API_KEY",
    "GELBOORU_USER_ID", "ANTI_BAN_PAUSE",
]
_env_before = {k: os.environ.get(k) for k in _ENV_KEYS}

import Rems_Dl  # noqa: E402
import core.notifications as notifications  # noqa: E402
import core.watchers as watchers  # noqa: E402
from workers.gelbooru import build_query  # noqa: E402

for _k, _v in _env_before.items():
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v


@pytest.fixture
def store(tmp_path, monkeypatch):
    paths = {
        "notif": str(tmp_path / "notifications.json"),
        "legacy": str(tmp_path / "legacy_missing.json"),
        "watchers": str(tmp_path / "watchers.json"),
    }
    monkeypatch.setattr(notifications, "NOTIF_FILE", paths["notif"])
    monkeypatch.setattr(notifications, "LEGACY_PIXIV_FILE", paths["legacy"])
    monkeypatch.setattr(notifications, "_migration_done", True)
    monkeypatch.setattr(watchers, "WATCHERS_FILE", paths["watchers"])
    monkeypatch.setattr(watchers, "ANTI_BAN_PAUSE", 0.0)
    return paths


def _post(i, rating="g"):
    return {"id": i, "rating": rating,
            "preview_url": f"https://img.example/{i}.jpg",
            "file_url": f"https://img.example/{i}.jpg"}


def _fake_fetch(pages, calls=None):
    """pages: {pid: [posts] | Exception}; missing pid -> empty page."""
    def f(api_tag, pid):
        if calls is not None:
            calls.append(pid)
        v = pages.get(pid)
        if isinstance(v, Exception):
            raise v
        return list(v) if v is not None else []
    return f


def _make(tags=None, **kw):
    data = {"source": "gelbooru", "tags": tags or ["1girl", "solo"],
            "ratings": [], "interval_minutes": 5}
    data.update(kw)
    w, err = watchers.create(data)
    assert err is None, err
    return w


def _check(pages, w, calls=None):
    monkey = _fake_fetch(pages, calls)
    orig = watchers._fetch_posts
    watchers._fetch_posts = monkey
    try:
        return watchers.check_watcher(w)
    finally:
        watchers._fetch_posts = orig


# ---------- CRUD ----------

def test_create_validation(store):
    w, err = watchers.create({"source": "nope", "tags": ["a"]})
    assert w is None and "unsupported" in err
    w, err = watchers.create({"source": "gelbooru", "tags": []})
    assert w is None
    w, err = watchers.create({"source": "gelbooru", "tags": ["a"],
                              "ratings": ["rating:x"]})
    assert w is None and "rating" in err
    w, err = watchers.create({"source": "gelbooru", "tags": ["a"],
                              "interval_minutes": 1})
    assert err is None and w["interval_minutes"] == 5  # clamped to min


def test_update_config_resets_toggle_preserves(store):
    w = _make()
    _check({0: [_post(10), _post(9)]}, w)  # baseline
    w = watchers.list_watchers("gelbooru")[0]
    assert w["initialized"] and w["last_seen_post_id"] == "10"

    # config change (new result set) -> fresh baseline, no stale checkpoint
    w2, err = watchers.update(w["watcher_id"], {"tags": ["landscape"]})
    assert err is None
    assert w2["initialized"] is False and w2["last_seen_post_id"] is None
    assert w2["scan_pid"] == 0

    # enable toggle keeps the checkpoint
    w2, err = watchers.update(w["watcher_id"], {"enabled": False})
    assert err is None
    assert w2["initialized"] is False and w2["enabled"] is False

    _, err = watchers.update("w_missing", {"enabled": True})
    assert err == "watcher not found"


# ---------- check_watcher ----------

def test_first_check_baselines_zero_notifs(store):
    w = _make()
    res = _check({0: [_post(10), _post(9)]}, w)
    assert res["new"] == 0 and res["error"] == ""
    w = watchers.list_watchers("gelbooru")[0]
    assert w["initialized"] and w["last_seen_post_id"] == "10"
    assert notifications.items("gelbooru") == []


def test_empty_first_check_stays_uninitialized(store):
    w = _make()
    res = _check({0: []}, w)
    assert res["new"] == 0
    w = watchers.list_watchers("gelbooru")[0]
    assert w["initialized"] is False

    # results arrive later -> still a baseline, not a flood
    res = _check({0: [_post(5)]}, w)
    assert res["new"] == 0
    w = watchers.list_watchers("gelbooru")[0]
    assert w["initialized"] and w["last_seen_post_id"] == "5"


def test_new_posts_notified_and_checkpoint_advances(store):
    w = _make()
    _check({0: [_post(10), _post(9)]}, w)  # baseline @10
    w = watchers.list_watchers("gelbooru")[0]
    res = _check({0: [_post(12), _post(11), _post(10), _post(9)]}, w)
    assert res["new"] == 2
    w = watchers.list_watchers("gelbooru")[0]
    assert w["last_seen_post_id"] == "12"
    ids = [i["external_id"] for i in notifications.items("gelbooru")]
    assert ids == ["12", "11"]


def test_checkpoint_stops_pagination(store):
    w = _make()
    _check({0: [_post(5)]}, w)  # baseline @5
    w = watchers.list_watchers("gelbooru")[0]
    calls = []
    res = _check({0: [_post(10), _post(9), _post(8)],
                  1: [_post(7), _post(6), _post(5)],
                  2: [_post(4)]}, w, calls)
    assert res["new"] == 5
    assert calls == [0, 1]  # page 2 never fetched once checkpoint seen


def test_cap_hit_error_keeps_checkpoint_and_resumes(store):
    w = _make()
    _check({0: [_post(1)]}, w)  # baseline @1
    w = watchers.list_watchers("gelbooru")[0]

    # checkpoint is deep below; feed never reaches it within the cap
    pages = {pid: [_post(10000 - pid * 100 - i) for i in range(100)]
             for pid in range(0, 10)}
    calls = []
    res = _check(pages, w, calls)
    assert res["error"].startswith("Too many new posts")
    assert calls == list(range(10))  # pid0 + 9 loop pages
    w = watchers.list_watchers("gelbooru")[0]
    assert w["last_seen_post_id"] == "1"  # checkpoint untouched
    assert w["scan_pid"] == 10
    assert w["scan_newest_id"] == "10000"  # cap write carries resume cursor
    assert notifications.items("gelbooru")  # collected posts were notified

    before = len(notifications.items("gelbooru"))
    # resume: feed exhausted past the cursor -> checkpoint advances, error clears
    res = _check({pid: [] for pid in range(10, 21)}, w, calls)
    assert res["error"] == "" and res["new"] == 0
    w = watchers.list_watchers("gelbooru")[0]
    assert w["scan_pid"] == 0 and w["last_seen_post_id"] == "10000"
    assert len(notifications.items("gelbooru")) == before  # dedup


def test_api_error_preserves_checkpoint(store):
    w = _make()
    _check({0: [_post(10)]}, w)  # baseline @10
    w = watchers.list_watchers("gelbooru")[0]
    res = _check({0: RuntimeError("gelbooru HTTP 429")}, w)
    assert "429" in res["error"]
    w = watchers.list_watchers("gelbooru")[0]
    assert w["last_seen_post_id"] == "10" and w["scan_pid"] == 0


def test_check_persists_runtime_exactly_once(store, monkeypatch):
    w = _make()
    _check({0: [_post(5)]}, w)  # baseline @5, real persistence
    w = watchers.list_watchers("gelbooru")[0]
    saves = []
    monkeypatch.setattr(watchers, "_save", lambda raw: saves.append(1))
    # paginated new-post check: no mid-scan write, end-of-check only
    _check({0: [_post(10), _post(9)],
            1: [_post(8), _post(7), _post(5)]}, w)
    assert len(saves) == 1


def test_two_watchers_same_post_two_notifications(store):
    wa = _make(tags=["1girl", "solo"])
    wb = _make(tags=["1girl", "solo"])
    _check({0: [_post(10)]}, wa)
    _check({0: [_post(10)]}, wb)
    wa, wb = watchers.list_watchers("gelbooru")
    _check({0: [_post(11), _post(10)]}, wa)
    _check({0: [_post(11), _post(10)]}, wb)
    items = notifications.items("gelbooru")
    assert len(items) == 2
    keys = {i["key"] for i in items}
    assert keys == {f"gelbooru:{wa['watcher_id']}:11",
                    f"gelbooru:{wb['watcher_id']}:11"}


# ---------- due_watchers ----------

def test_due_watchers_pure(store):
    w = _make(interval_minutes=10)
    now = time.time()
    watchers._set_runtime(w["watcher_id"], {"last_checked_at": now, "enabled": True})
    assert watchers.due_watchers(now + 5 * 60) == []
    due = watchers.due_watchers(now + 11 * 60)
    assert len(due) == 1 and due[0]["watcher_id"] == w["watcher_id"]
    watchers._set_runtime(w["watcher_id"], {"enabled": False})
    assert watchers.due_watchers(now + 60 * 60) == []


def test_emit_payload(store):
    events = []
    watchers._emit_fn = lambda p: events.append(p)
    try:
        notifications.ingest("gelbooru", [{"external_id": "7"}])
        w = _make()
        watchers._emit(w, 2)
    finally:
        watchers._emit_fn = None
    assert events[0]["source"] == "gelbooru" and events[0]["new"] == 2
    assert events[0]["unread"] >= 1
    assert events[0]["unread_by_source"].get("gelbooru") >= 1


# ---------- manual check-now ----------

def test_check_now_busy_guard(store):
    assert watchers._manual_lock.acquire(blocking=False)
    try:
        assert watchers.check_now() is False
    finally:
        watchers._manual_lock.release()


def test_check_now_runs_enabled_watchers_immediately(store):
    w = _make()
    _check({0: [_post(5)]}, w)  # baseline @5
    off = _make(tags=["landscape"])
    watchers.update(off["watcher_id"], {"enabled": False})
    events = []
    watchers._emit_fn = lambda p: events.append(p)
    orig = watchers._fetch_posts
    watchers._fetch_posts = _fake_fetch({0: [_post(9), _post(5)]})
    try:
        assert watchers.check_now("gelbooru") is True
        deadline = time.time() + 5
        # wait for the run AND its lock release — a leaked lock would
        # poison the next test's busy guard
        while time.time() < deadline and (not events or watchers._manual_lock.locked()):
            time.sleep(0.01)
    finally:
        watchers._fetch_posts = orig
        watchers._emit_fn = None
    assert events and events[0]["new"] == 1
    w = watchers.list_watchers("gelbooru")[0]
    assert w["last_seen_post_id"] == "9"
    by_tag = {tuple(x["tags"]): x for x in watchers.list_watchers("gelbooru")}
    assert by_tag[("landscape",)]["last_checked_at"] == 0  # disabled = skipped


# ---------- notification store semantics ----------

def test_summary_one_pass_matches_components(store):
    notifications.ingest("gelbooru", [{"external_id": "1", "watcher_id": "w"}])
    notifications.ingest("rule34", [{"external_id": "9"}])
    out, unread, by = notifications.summary("gelbooru")
    assert out == notifications.items("gelbooru")
    assert unread == notifications.unread() == 2
    assert by == notifications.unread_by_source() == {"gelbooru": 1, "rule34": 1}
    assert notifications.summary()[0] == notifications.items()


def test_source_filter_global_unread_and_mark_read(store):
    notifications.ingest("gelbooru", [{"external_id": "1", "watcher_id": "w1"},
                                      {"external_id": "2", "watcher_id": "w1"}])
    notifications.ingest("rule34", [{"external_id": "9"}])  # future source
    assert len(notifications.items("gelbooru")) == 2
    assert len(notifications.items("rule34")) == 1
    assert notifications.unread() == 3
    assert notifications.unread_by_source() == {"gelbooru": 2, "rule34": 1}

    key = "gelbooru:w1:1"
    left = notifications.mark_read("gelbooru", [key])
    assert left == 2  # global: rule34 1 + gelbooru 1
    left = notifications.mark_read("gelbooru", ["2"])  # bare external_id
    assert left == 1
    assert notifications.unread("gelbooru") == 0


def test_clear_remembered_not_renotified(store):
    fresh = notifications.ingest("gelbooru", [{"external_id": "5", "watcher_id": "w"}])
    assert len(fresh) == 1
    notifications.clear("gelbooru")
    assert notifications.items("gelbooru") == []
    again = notifications.ingest("gelbooru", [{"external_id": "5", "watcher_id": "w"}])
    assert again == []


def test_legacy_pixiv_migration(store, tmp_path, monkeypatch):
    legacy_path = store["legacy"]
    with open(legacy_path, "w") as f:
        json.dump({"items": [{"work_id": "123", "title": "old", "read": False}],
                   "seen": ["456"], "last_check": 1700000000}, f)
    monkeypatch.setattr(notifications, "_migration_done", False)
    notifications._load()  # first access folds the legacy file in

    items = notifications.items("pixiv")
    assert [i["key"] for i in items] == ["pixiv::123"]
    assert items[0]["source"] == "pixiv" and items[0]["external_id"] == "123"
    assert "pixiv::456" in notifications.seen_keys("pixiv")
    assert notifications.last_check("pixiv") == 1700000000
    assert not os.path.exists(legacy_path)
    assert os.path.exists(legacy_path + ".migrated")


# ---------- endpoints ----------

def _client():
    Rems_Dl.app.config["TESTING"] = True
    c = Rems_Dl.app.test_client()
    c.environ_base["HTTP_USER_AGENT"] = "RemsDlDesktopApp test"
    return c


def test_notifications_endpoints(store):
    c = _client()
    # startup dot fetch: no source -> counts only
    d = c.get("/api/notifications").get_json()
    assert d["items"] == [] and d["unread"] == 0 and d["status"] == {}

    notifications.ingest("gelbooru", [{"external_id": "1", "watcher_id": "w"},
                                      {"external_id": "2", "watcher_id": "w"}])
    d = c.get("/api/notifications?source=gelbooru").get_json()
    assert len(d["items"]) == 2
    assert d["unread"] == 2 and d["unread_by_source"] == {"gelbooru": 2}
    assert d["status"] == {}  # no pixiv status row for a gelbooru page

    d = c.get("/api/notifications?source=pixiv").get_json()
    assert d["items"] == [] and "last_check" in d["status"]

    assert c.post("/api/notifications/read", json={}).status_code == 400
    d = c.post("/api/notifications/read",
               json={"source": "gelbooru"}).get_json()
    assert d["unread"] == 0
    assert c.post("/api/notifications/clear", json={}).status_code == 400
    d = c.post("/api/notifications/clear",
               json={"source": "gelbooru"}).get_json()
    assert d["unread_by_source"] == {}


def test_watchers_endpoints(store):
    c = _client()
    assert c.get("/api/watchers?source=gelbooru").get_json()["watchers"] == []
    assert c.post("/api/watchers", json={"source": "x", "tags": ["a"]}
                  ).status_code == 400
    assert c.post("/api/watchers", json={"source": "gelbooru", "tags": []}
                  ).status_code == 400

    d = c.post("/api/watchers", json={"source": "gelbooru",
                                      "tags": ["1girl"],
                                      "ratings": ["rating:g"],
                                      "interval_minutes": 15}).get_json()
    wid = d["watcher"]["watcher_id"]
    assert d["watcher"]["interval_minutes"] == 15

    assert c.post(f"/api/watchers/{wid}", json={"enabled": False}
                  ).get_json()["watcher"]["enabled"] is False
    assert c.post("/api/watchers/w_nope", json={"enabled": False}
                  ).status_code == 404
    assert c.delete("/api/watchers/w_nope").status_code == 404
    assert c.delete(f"/api/watchers/{wid}").get_json()["success"] is True
    assert c.get("/api/watchers").get_json()["watchers"] == []

    # check-now: busy lock -> started:false, no thread spawned
    assert watchers._manual_lock.acquire(blocking=False)
    try:
        assert c.post("/api/watchers/check_now", json={}).get_json() == {"started": False}
    finally:
        watchers._manual_lock.release()


# ---------- shared query builder ----------

def test_build_query_and_and_negation():
    assert build_query("1girl", "")[0] == "1girl"
    api, allowed, display = build_query("1girl", "rating:g")
    assert api == "1girl rating:general"
    assert allowed == {"general"} and display == "Safe"
    # multi-rating subset: negate the complement (gelbooru can't OR ratings)
    api, allowed, _ = build_query("1girl", "rating:g rating:s")
    assert api == "1girl -rating:questionable -rating:explicit"
    assert allowed == {"general", "sensitive"}
    # all four = no filter at all
    assert build_query("1girl", "rating:g rating:s rating:q rating:e")[0] == "1girl"
