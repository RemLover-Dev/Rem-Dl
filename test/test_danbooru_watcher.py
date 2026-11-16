import datetime
import os
import time

import pytest

pytestmark = pytest.mark.usefixtures("bypass_access_guard")

# same env snapshot/restore dance as test_gelbooru_watcher
_ENV_KEYS = ["USE_PROXY", "PROXY_URL", "VERIFY_TLS", "DANBOORU_LOGIN",
             "DANBOORU_API_KEY", "ANTI_BAN_PAUSE"]
_env_before = {k: os.environ.get(k) for k in _ENV_KEYS}

import Rems_Dl
from core import (
    notifications,
    watchers,
)
from workers.gelbooru import build_query

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


def _post(i, rating="g", created=None):
    out = {"id": i, "rating": rating,
           "preview_file_url": f"https://cdn.example/{i}.jpg",
           "file_url": f"https://cdn.example/{i}.jpg"}
    if created is not None:
        out["created_at"] = datetime.datetime.fromtimestamp(
            created, datetime.timezone.utc).isoformat()
    return out


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
    data = {"source": "danbooru", "tags": tags or ["1girl", "solo"],
            "ratings": [], "interval_minutes": 5}
    data.update(kw)
    w, err = watchers.create(data)
    assert err is None, err
    return w


def _check(pages, w, calls=None):
    monkey = _fake_fetch(pages, calls)
    orig = watchers.danbooru.fetch
    watchers.danbooru.fetch = monkey
    try:
        return watchers.check_watcher(w)
    finally:
        watchers.danbooru.fetch = orig


# ---------- query builder ----------

def test_danbooru_query_rating_comma_list():
    q = watchers.danbooru.query(["1girl", "-male"],
                                 ["rating:g", "rating:s"])
    assert q == "1girl -male rating:g,s"


def test_danbooru_query_all_ratings_drops_clause():
    q = watchers.danbooru.query(["1girl"],
                                 list(watchers.RATING_TOKENS))
    assert q == "1girl"


def test_danbooru_query_no_ratings():
    assert watchers.danbooru.query(["1girl", "solo"], []) == "1girl solo"


def test_danbooru_ts_parses_iso():
    now = time.time()
    p = _post(1, created=now)
    assert abs(watchers._ts_flex(p["created_at"]) - now) < 2
    assert watchers._ts_flex(None) == 0.0


# ---------- check_watcher ----------

def test_first_check_baselines_zero_notifs(store):
    w = _make()
    res = _check({0: [_post(10), _post(9)]}, w)
    assert res["new"] == 0 and res["error"] == ""
    w = watchers.list_watchers("danbooru")[0]
    assert w["initialized"] and w["last_seen_post_id"] == "10"
    assert notifications.items("danbooru") == []


def test_baseline_notifies_posts_newer_than_creation(store):
    w = _make()
    w["baseline_at"] = time.time() - 3600
    now = time.time()
    res = _check({0: [_post(7, created=now - 60),       # after creation
                      _post(6, created=now - 7200)]},  # before creation
                 w)
    assert res["new"] == 1
    ids = [i["external_id"] for i in notifications.items("danbooru")]
    assert ids == ["7"]
    item = notifications.items("danbooru")[0]
    assert item["thumb"] == "https://cdn.example/7.jpg"
    assert item["rating"] == "g"
    assert item["metadata"]["url"] == "https://cdn.example/7.jpg"


def test_empty_first_check_stays_uninitialized(store):
    w = _make()
    res = _check({0: []}, w)
    assert res["new"] == 0
    w = watchers.list_watchers("danbooru")[0]
    assert w["initialized"] is False
    res = _check({0: [_post(5)]}, w)
    assert res["new"] == 0  # still a baseline, not a flood
    w = watchers.list_watchers("danbooru")[0]
    assert w["initialized"] and w["last_seen_post_id"] == "5"


def test_new_posts_notified_and_checkpoint_advances(store):
    w = _make()
    _check({0: [_post(10), _post(9)]}, w)
    w = watchers.list_watchers("danbooru")[0]
    res = _check({0: [_post(12), _post(11), _post(10), _post(9)]}, w)
    assert res["new"] == 2
    w = watchers.list_watchers("danbooru")[0]
    assert w["last_seen_post_id"] == "12"
    ids = [i["external_id"] for i in notifications.items("danbooru")]
    assert ids == ["12", "11"]


def test_checkpoint_stops_pagination(store):
    w = _make()
    _check({0: [_post(5)]}, w)
    w = watchers.list_watchers("danbooru")[0]
    calls = []
    res = _check({0: [_post(10), _post(9), _post(8)],
                  1: [_post(7), _post(6), _post(5)],
                  2: [_post(4)]}, w, calls)
    assert res["new"] == 5
    assert calls == [0, 1]  # page 2 never fetched once checkpoint seen


def test_api_error_preserves_checkpoint(store):
    w = _make()
    _check({0: [_post(10)]}, w)
    w = watchers.list_watchers("danbooru")[0]
    res = _check({0: RuntimeError("danbooru HTTP 429")}, w)
    assert "429" in res["error"]
    w = watchers.list_watchers("danbooru")[0]
    assert w["last_seen_post_id"] == "10" and w["scan_pid"] == 0


def test_two_watchers_same_post_two_notifications(store):
    wa = _make(tags=["1girl", "solo"])
    wb = _make(tags=["1girl", "solo"])
    _check({0: [_post(10)]}, wa)
    _check({0: [_post(10)]}, wb)
    wa, wb = watchers.list_watchers("danbooru")
    _check({0: [_post(11), _post(10)]}, wa)
    _check({0: [_post(11), _post(10)]}, wb)
    items = notifications.items("danbooru")
    assert len(items) == 2
    keys = {i["key"] for i in items}
    assert keys == {f"danbooru:{wa['watcher_id']}:11",
                    f"danbooru:{wb['watcher_id']}:11"}
