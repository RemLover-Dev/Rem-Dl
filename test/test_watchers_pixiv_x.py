"""Pixiv tag watcher and X account watcher adapters, plus the engine hooks
they need (keep(w, post) binding and fetch_pages)."""

import time
from types import SimpleNamespace

import pytest

from core import notifications, watchers
from core.watchers import pixiv, x_twitter


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(notifications, "NOTIF_FILE",
                        str(tmp_path / "notifications.json"))
    monkeypatch.setattr(notifications, "LEGACY_PIXIV_FILE",
                        str(tmp_path / "legacy_missing.json"))
    monkeypatch.setattr(notifications, "_migration_done", True)
    monkeypatch.setattr(watchers, "WATCHERS_FILE",
                        str(tmp_path / "watchers.json"))
    monkeypatch.setattr(watchers, "ANTI_BAN_PAUSE", 0.0)
    return tmp_path


def _fake_adapter(monkeypatch, **mod):
    fake = SimpleNamespace(**mod)
    monkeypatch.setitem(watchers._ADAPTERS, "fake", fake)
    monkeypatch.setattr(watchers, "SOURCES", tuple(watchers._ADAPTERS))
    return fake


def _make(source="fake", tags=("rem",)):
    w, err = watchers.create({"source": source, "tags": list(tags)})
    assert err is None, err
    return w


def _stored(source="fake"):
    return watchers.list_watchers(source)[0]


# ---------- engine: keep(w, post) ----------

def test_keep_receives_the_watcher(store, monkeypatch):
    seen = []

    pages = [[{"id": 5}], [{"id": 6}, {"id": 2}, {"id": 5}]]

    def keep(w, p):
        seen.append((list(w["tags"]), p["id"]))
        return p["id"] != 2

    _fake_adapter(
        monkeypatch, LIMIT=2,
        query=lambda tags, ratings: "q",
        fetch=lambda q, pid: pages.pop(0) if pages else [],
        fields=lambda p: ("", "", ""),
        keep=keep,
    )
    w = _make()
    assert watchers.check_watcher(w) == {"new": 0, "error": ""}  # baseline

    res = watchers.check_watcher(_stored())
    assert res == {"new": 1, "error": ""}
    assert seen == [(["rem"], 6), (["rem"], 2)]
    assert [i["external_id"] for i in notifications.items("fake")] == ["6"]


# ---------- engine: fetch_pages ----------

def test_fetch_pages_replaces_paging(store, monkeypatch):
    calls = []

    def fetch_pages(api_tag, w):
        calls.append((api_tag, w["tags"]))
        return [{"id": 9}, {"id": 8}]

    _fake_adapter(monkeypatch, LIMIT=2,
                  query=lambda tags, ratings: "q",
                  fields=lambda p: ("", "", ""),
                  fetch_pages=fetch_pages)
    w = _make()
    assert watchers.check_watcher(w) == {"new": 0, "error": ""}
    assert _stored()["last_seen_post_id"] == "9"

    _fake_adapter(monkeypatch, LIMIT=2,
                  query=lambda tags, ratings: "q",
                  fields=lambda p: ("", "", ""),
                  fetch_pages=lambda api_tag, w: [{"id": 11}, {"id": 10}])
    res = watchers.check_watcher(_stored())
    assert res == {"new": 2, "error": ""}
    assert _stored()["last_seen_post_id"] == "11"
    assert calls == [("q", ["rem"])]


# ---------- pixiv ----------

ILLUST = {
    "id": 42,
    "create_date": "2026-01-02T03:04:05+09:00",
    "x_restrict": 0,
    "image_urls": {"square_medium": "https://i.pximg.net/sq.jpg",
                   "large": "https://i.pximg.net/lg.jpg"},
    "meta_single_page": {"original_image_url": "https://i.pximg.net/orig.jpg"},
    "tags": [{"name": "Rem"}, {"name": "1girl"}],
}


def test_pixiv_query_strips_exclusions():
    assert pixiv.query(["rem", "-ai", " 1girl "], []) == "rem 1girl"


def test_pixiv_fetch_offsets_and_stamps_created_at(monkeypatch):
    calls = []

    class FakeAPI:
        def _call(self, endpoint, params):
            calls.append((endpoint, params))
            return {"illusts": [dict(ILLUST)]}

    monkeypatch.setattr(pixiv, "_get_api", lambda: FakeAPI())
    posts = pixiv.fetch("rem 1girl", 2)
    endpoint, params = calls[0]
    assert endpoint == pixiv.API
    assert params["word"] == "rem 1girl"
    assert params["sort"] == "date_desc"
    assert params["offset"] == 2 * pixiv.LIMIT
    assert posts[0]["created_at"] == ILLUST["create_date"]


def test_pixiv_fetch_rejects_empty_tag(monkeypatch):
    with pytest.raises(RuntimeError, match="enter at least one tag"):
        pixiv.fetch("", 0)


def test_pixiv_keep_ratings_and_exclusions():
    safe = {"x_restrict": 0, "tags": [{"name": "1girl"}]}
    r18 = {"x_restrict": 1, "tags": [{"name": "1girl"}]}
    r18g = {"x_restrict": 2, "tags": [{"name": "guro"}]}

    assert pixiv.keep({"ratings": []}, r18g)  # all ratings
    assert pixiv.keep({"ratings": ["rating:g"]}, safe)
    assert not pixiv.keep({"ratings": ["rating:g"]}, r18)
    assert not pixiv.keep({"ratings": ["rating:q"]}, safe)
    assert pixiv.keep({"ratings": ["rating:e"]}, r18g)
    assert not pixiv.keep({"ratings": [], "tags": ["-guro"]}, r18g)
    assert pixiv.keep({"ratings": [], "tags": ["-guro"]}, safe)


def test_pixiv_fields_picks_preview_and_original():
    thumb, rating, url = pixiv.fields(ILLUST)
    assert thumb == "https://i.pximg.net/sq.jpg"
    assert rating == ""
    assert url == "https://i.pximg.net/orig.jpg"


# ---------- x ----------

class _Photo:
    def __init__(self, url):
        self.url = url


class _Tweet:
    def __init__(self, tid, username="Artist", photos=("a.jpg",), retweet=None):
        self.id = tid
        self.date = None
        self.retweetedTweet = retweet
        self.user = SimpleNamespace(username=username)
        self.media = SimpleNamespace(
            photos=[_Photo(f"https://pbs.twimg.com/media/{p}") for p in photos])


def test_x_query_normalizes_handle():
    assert (x_twitter.query(["@Artist"], [])
            == "from:Artist filter:media -filter:retweets")
    assert (x_twitter.query(["x.com/Artist/"], [])
            == "from:Artist filter:media -filter:retweets")
    with pytest.raises(RuntimeError, match="enter an account handle"):
        x_twitter.query([], [])


def test_x_since_uses_checkpoint_snowflake():
    ts = 1_700_000_000
    cid = (ts * 1000 - x_twitter.SNOWFLAKE_EPOCH_MS) << 22
    assert x_twitter._since({"last_seen_post_id": str(cid)}) == ts - x_twitter.OVERLAP
    # no checkpoint yet: the baseline window
    assert x_twitter._since({"baseline_at": ts}) == ts - x_twitter.OVERLAP


def test_x_fetch_pages_normalizes_tweets(monkeypatch):
    async def fake_walk(q, handle, since):
        assert handle == "artist"
        return [_Tweet(123), _Tweet(124, photos=())]

    monkeypatch.setattr(x_twitter, "_walk", fake_walk)
    posts = x_twitter.fetch_pages("from:Artist filter:media",
                                  {"tags": ["@artist"], "baseline_at": time.time()})
    assert [p["id"] for p in posts] == ["123", "124"]
    assert posts[0]["created_at"] == 0
    assert posts[0]["thumb"] == "https://pbs.twimg.com/media/a.jpg"
    assert posts[1]["photos"] == []


def test_x_fields_sizes_the_thumb():
    p = {"thumb": "https://pbs.twimg.com/media/a.jpg",
         "photos": ["https://pbs.twimg.com/media/a.jpg"]}
    thumb, rating, url = x_twitter.fields(p)
    assert thumb == "https://pbs.twimg.com/media/a.jpg?name=360x360"
    assert rating == ""
    assert url == "https://pbs.twimg.com/media/a.jpg?name=orig"


# ---------- engine: error naming ----------

def test_errors_name_the_site_with_capital(store, monkeypatch):
    def boom(q, pid):
        raise RuntimeError("HTTP 403")

    _fake_adapter(monkeypatch, LIMIT=2, NAME="Fake Site",
                  query=lambda tags, ratings: "q",
                  fetch=boom, fields=lambda p: ("", "", ""))
    res = watchers.check_watcher(_make())
    assert res["error"] == "Fake Site: HTTP 403"


def test_danbooru_403_names_the_site(store, monkeypatch):
    from core.watchers import danbooru

    class _Resp:
        status_code = 403
        def raise_for_status(self):
            raise AssertionError("must not reach raise_for_status")

    monkeypatch.setattr(danbooru, "get", lambda *a, **k: _Resp())
    res = watchers.check_watcher(
        _make("danbooru", tags=("rem",)))
    assert res["error"] == "Danbooru: HTTP 403"
