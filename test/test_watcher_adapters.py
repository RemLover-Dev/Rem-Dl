"""Per-site watcher adapter tests: query builders, fields mappings,
keep() filters. The generic checkpoint engine is covered by the
gelbooru/danbooru watcher tests."""

import json
import os

import pytest

pytestmark = pytest.mark.usefixtures("bypass_access_guard")

import Rems_Dl
from core import watchers
from core.watchers import (
    anime_dl,
    danbooru,
    eshuushuu,
    gsbooru,
    konachan,
    rule34,
    zerochan,
)


# ---------- query builders ----------

def test_safebooru_query_reuses_dapi_negation_trick():
    q = watchers.safebooru.query(["rem", "-long_hair"],
                                 ["rating:g", "rating:s"])
    assert q.startswith("rem -long_hair")
    assert "-rating:" in q  # gelbooru OR-trick excludes the unselected


def test_moe_query_single_rating_server_side():
    assert konachan.query(["touhou"], ["rating:s"]) == "touhou rating:s"
    q = konachan.query(["touhou"], ["rating:s", "rating:e"])
    assert q == "touhou"  # multi -> local keep() filter


def test_moe_query_multi_rating_no_metatag_local_keep():
    q = konachan.query(["touhou", "-comic"], ["rating:s", "rating:e"])
    assert q == "touhou -comic"
    assert not konachan.keep({"ratings": ["rating:s", "rating:e"]},
                             {"rating": "q"})
    assert konachan.keep({"ratings": ["rating:s", "rating:e"]},
                         {"rating": "s"})
    assert konachan.keep({"ratings": []}, {"rating": "q"})


def test_rule34_query_space_joined_list():
    assert rule34.query(["rem", "-comic"], []) == ["rem -comic"]


def test_zerochan_query_drops_negations_and_encodes():
    q = zerochan.query(["rem", "-male"], [])
    assert q == "rem"
    assert "+" in zerochan.query(["long hair"], [])


def test_anime_dl_query_drops_negations_keeps_multiword():
    assert anime_dl.query(["rem", "-male"], []) == "rem"
    assert anime_dl.query(["yosuga no sora", "-kasugano sora"], []) == "yosuga no sora"


def test_anime_dl_fetch_posts_and_http_error(monkeypatch):
    class _Resp:
        status_code = 200
        def json(self):
            return {"posts": [{"id": 1, "md5": "a" * 32}]}

    class _Bad:
        status_code = 503

    seen = {}
    def fake_get(url, **kw):
        seen["url"], seen["params"] = url, kw.get("params")
        return _Resp()

    monkeypatch.setattr(anime_dl, "_cf_get", fake_get)
    assert anime_dl.fetch("yosuga no sora", 2) == [{"id": 1, "md5": "a" * 32}]
    assert seen["url"].endswith("/api/v3/posts")
    assert seen["params"]["page"] == 2 and seen["params"]["limit"] == 80
    assert seen["params"]["search_tag"] == "yosuga no sora"

    monkeypatch.setattr(anime_dl, "_cf_get", lambda *a, **k: _Bad())
    with pytest.raises(RuntimeError):
        anime_dl.fetch("x", 0)


def test_anime_dl_fields_md5_preview():
    thumb, rating, url = anime_dl.fields(
        {"id": 907317, "md5": "812a6a999d015d35326345c8202b1e2d"})
    assert thumb == ("https://opreviews.anime-pictures.net/812/"
                     "812a6a999d015d35326345c8202b1e2d_sp.png")
    assert rating == ""
    assert url == "https://anime-pictures.net/posts/907317"
    assert anime_dl.fields({"id": 1, "md5": ""})[0] == ""


def test_eshuushuu_query_resolves_cached_ids(tmp_path, monkeypatch):
    db = tmp_path / "eshuushuu_tags.json"
    db.write_text(json.dumps([{"title": "Long Hair", "tag_id": 42},
                              {"title": "rem", "tag_id": 7}]))
    monkeypatch.setattr(eshuushuu, "TAGS_DB", str(db))
    # underscores match spaced titles; dashed tokens are dropped
    assert eshuushuu.query(["long_hair", "-rem"], []) == "42"
    with pytest.raises(RuntimeError):
        eshuushuu.query(["no_such_tag_xyz"], [])


def test_gsbooru_query_comma_rating_and_local_exclusions():
    tag, rating = gsbooru.query(["rem", "-bad_tag"], ["rating:g", "rating:s"])
    assert tag == "rem"
    assert rating == "g,s"
    assert gsbooru.query(["rem"], ["rating:g", "rating:s", "rating:q"]) == ("rem", "")
    assert not gsbooru.keep({"tags": ["-bad_tag"]},
                            {"tag_string": "rem bad_tag okay"})
    assert gsbooru.keep({"tags": ["-bad_tag"]}, {"tag_string": "rem okay"})


# ---------- fields mappings ----------

def test_danbooru_fields_preview_file_url():
    thumb, rating, url = danbooru.fields({"preview_file_url": "t", "rating": "g",
                                          "file_url": "u"})
    assert (thumb, rating, url) == ("t", "g", "u")


def test_gsbooru_fields_int_rating():
    thumb, rating, url = gsbooru.fields({"file_url": "u", "rating": 2})
    assert rating == "q" and thumb == "u" and url == "u"
    assert gsbooru.fields({"file_url": "u", "rating": 9})[1] == ""


def test_eshuushuu_fields_prefixes_host():
    thumb, rating, url = eshuushuu.fields({"thumb": "/thumbs/2020-01-01-99.webp"})
    assert thumb == "https://e-shuushuu.net/thumbs/2020-01-01-99.webp"
    assert rating == "" and url == ""


# ---------- rule34 fetch dict conversion ----------

class _FakePost:
    def __init__(self):
        self.id = 123
        self.preview = "prev"
        self.sample = "samp"
        self.image = "full"
        self.change = 1700000000


def test_rule34_fetch_converts_post_objects(monkeypatch):
    from core.watchers import rule34 as r34

    class _FakeClient:
        def __init__(self):
            self.api_key = ""
            self.user_id = 0

        def search(self, tags, page_id=0, limit=1000):
            assert tags == ["rem -comic"]
            assert page_id == 2
            return [_FakePost()]

    import rule34Py
    monkeypatch.setattr(rule34Py, "rule34Py", _FakeClient)
    posts = r34.fetch(["rem -comic"], 2)
    assert posts == [{"id": 123, "preview_url": "prev", "sample_url": "samp",
                      "file_url": "full", "created_at": 1700000000}]
