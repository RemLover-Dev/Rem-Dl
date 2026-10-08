import json
import os
import time

import Rems_Dl

HEADERS = {"User-Agent": "RemsDlDesktopApp/1.0"}


def _write_cache(tmp_path, tags, age_s=0):
    path = os.path.join(str(tmp_path), "waifu.im_tags.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(tags, f)
    if age_s:
        old = time.time() - age_s
        os.utime(path, (old, old))
    return path


def test_cache_fresh_when_live_shaped_and_recent(tmp_path, monkeypatch):
    monkeypatch.setattr(Rems_Dl, "DATABASE_DIR", str(tmp_path))
    _write_cache(tmp_path, [{"name": "Maid", "slug": "maid", "imageCount": 10}])
    tags, fresh = Rems_Dl._waifu_tag_cache()
    assert fresh is True
    assert tags[0]["slug"] == "maid"


def test_bundled_seed_without_imagecount_is_stale(tmp_path, monkeypatch):
    # the shipped waifu.im_tags.json only has name+slug — it must live-refresh once
    monkeypatch.setattr(Rems_Dl, "DATABASE_DIR", str(tmp_path))
    _write_cache(tmp_path, [{"name": "Maid", "slug": "maid"}])
    _, fresh = Rems_Dl._waifu_tag_cache()
    assert fresh is False


def test_old_cache_is_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(Rems_Dl, "DATABASE_DIR", str(tmp_path))
    _write_cache(tmp_path, [{"name": "Maid", "slug": "maid", "imageCount": 1}],
                 age_s=Rems_Dl.WAIFU_TAGS_TTL + 60)
    _, fresh = Rems_Dl._waifu_tag_cache()
    assert fresh is False


def test_endpoint_serves_cache_without_hitting_api(tmp_path, monkeypatch):
    monkeypatch.setattr(Rems_Dl, "DATABASE_DIR", str(tmp_path))
    monkeypatch.setattr(Rems_Dl, "WAIFU_TAGS_DB", [])
    monkeypatch.setattr(Rems_Dl, "WAIFU_TAG_MAP", {})
    _write_cache(tmp_path, [{"name": "Maid", "slug": "maid", "imageCount": 10},
                            {"name": "Waifu", "slug": "waifu", "imageCount": 5}])

    def boom(*a, **k):
        raise AssertionError("fresh cache must not trigger a live fetch")

    monkeypatch.setattr(Rems_Dl, "get_session", boom)
    resp = Rems_Dl.app.test_client().post("/api/tags/waifu", headers=HEADERS, json={"net_config": {}})
    assert resp.status_code == 200
    assert resp.get_json() == ["maid", "waifu"]


def test_stale_cache_merges_add_only(tmp_path, monkeypatch):
    monkeypatch.setattr(Rems_Dl, "DATABASE_DIR", str(tmp_path))
    monkeypatch.setattr(Rems_Dl, "WAIFU_TAGS_DB", [])
    monkeypatch.setattr(Rems_Dl, "WAIFU_TAG_MAP", {})
    monkeypatch.setattr(Rems_Dl.shared, "WAIFU_TAG_MAP", {})
    _write_cache(tmp_path, [{"name": "Old", "slug": "old", "imageCount": 1}],
                 age_s=Rems_Dl.WAIFU_TAGS_TTL + 60)

    class _Resp:
        def json(self):
            return {"items": [{"name": "Maid", "slug": "maid", "imageCount": 9},
                              {"name": "New", "slug": "new", "imageCount": 3}]}

    class _Sess:
        def get(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(Rems_Dl, "get_session", lambda *a, **k: _Sess())
    resp = Rems_Dl.app.test_client().post("/api/tags/waifu", headers=HEADERS, json={"net_config": {}})
    # response answers from disk immediately (no blocking live pull)
    assert resp.get_json() == ["old"]
    # the refresh runs in the background — poll until it lands on disk
    path = os.path.join(str(tmp_path), "waifu.im_tags.json")
    slugs = set()
    for _ in range(100):
        slugs = {t["slug"] for t in json.load(open(path, encoding="utf-8"))}
        if {"maid", "new"} <= slugs:
            break
        time.sleep(0.05)
    assert "old" in slugs, "cached tags are never dropped"
    assert {"maid", "new"} <= slugs, "new live tags are added"


def test_query_filter_hits_cached_slugs(tmp_path, monkeypatch):
    monkeypatch.setattr(Rems_Dl, "DATABASE_DIR", str(tmp_path))
    monkeypatch.setattr(Rems_Dl, "WAIFU_TAGS_DB", [])
    monkeypatch.setattr(Rems_Dl, "WAIFU_TAG_MAP", {})
    _write_cache(tmp_path, [{"name": "Maid", "slug": "maid", "imageCount": 10},
                            {"name": "MILF", "slug": "milf", "imageCount": 5}])

    def boom(*a, **k):
        raise AssertionError("fresh cache must not trigger a live fetch")

    monkeypatch.setattr(Rems_Dl, "get_session", boom)
    resp = Rems_Dl.app.test_client().post("/api/tags/waifu", headers=HEADERS, json={"query": "mi"})
    assert resp.get_json() == ["milf"]
