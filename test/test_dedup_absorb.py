"""phash kill carries the duplicate's tags + site into the original's record."""
import os
from types import SimpleNamespace

import pytest

from core import shared


@pytest.fixture
def lib(tmp_path, monkeypatch):
    (tmp_path / "pixiv" / "art").mkdir(parents=True)
    monkeypatch.setattr(shared, "GALLERY_FILE", str(tmp_path / "gallery.json"))
    monkeypatch.setattr(shared, "MASTER_FOLDER", str(tmp_path))
    monkeypatch.setattr(shared, "_gallery_cache",
                        {"data": {"images": []}, "filenames": set(), "dirty": 0})
    return tmp_path


def _original(lib, filename="a.jpg"):
    shared.add_to_gallery("pixiv", filename, f"pixiv/art/{filename}",
                          ["blue_hair"], ["alice"])
    return os.path.join(str(lib), "pixiv", "art", filename)


def _dup(path):
    return SimpleNamespace(is_duplicate=True, matched_path=path)


def test_absorb_unions_tags_and_appends_source(lib):
    fp = _original(lib)

    assert shared.absorb_duplicate(_dup(fp), "danbooru", ["red_hair", "blue_hair"], ["bob"])

    img = shared._gallery_cache["data"]["images"][0]
    assert img["sources"] == ["pixiv", "danbooru"]
    assert img["tags"]["tag"] == ["blue_hair", "red_hair"]
    assert img["tags"]["artist"] == ["alice", "bob"]
    # primary site untouched — filters, folders and cards all read it
    assert img["site"] == "pixiv"


def test_absorb_is_idempotent(lib):
    fp = _original(lib)
    shared.absorb_duplicate(_dup(fp), "danbooru", ["red_hair"], ["bob"])
    shared.absorb_duplicate(_dup(fp), "danbooru", ["red_hair"], ["bob"])

    img = shared._gallery_cache["data"]["images"][0]
    assert img["sources"] == ["pixiv", "danbooru"]
    assert img["tags"]["tag"] == ["blue_hair", "red_hair"]
    assert img["tags"]["artist"] == ["alice", "bob"]


def test_absorb_collects_third_site(lib):
    fp = _original(lib)
    shared.absorb_duplicate(_dup(fp), "danbooru", ["t1"], [])
    shared.absorb_duplicate(_dup(fp), "zerochan", ["t2"], [])

    img = shared._gallery_cache["data"]["images"][0]
    assert img["sources"] == ["pixiv", "danbooru", "zerochan"]
    assert img["tags"]["tag"] == ["blue_hair", "t1", "t2"]


def test_absorb_matches_by_filename_when_filepath_empty(lib):
    fp = _original(lib)
    shared._gallery_cache["data"]["images"][0]["filepath"] = ""

    assert shared.absorb_duplicate(_dup(fp), "danbooru", [], [])

    assert shared._gallery_cache["data"]["images"][0]["sources"] == ["pixiv", "danbooru"]


def test_absorb_noop_on_missing_or_nomatch(lib):
    assert shared.absorb_duplicate(None, "danbooru", ["t"], []) is False
    assert shared.absorb_duplicate(
        SimpleNamespace(is_duplicate=False, matched_path=None), "x", [], []) is False
    assert shared.absorb_duplicate(
        SimpleNamespace(is_duplicate=True, matched_path="/nope/missing.jpg"),
        "danbooru", ["t"], []) is False
    # no phantom record invented for an unknown match
    assert shared._gallery_cache["data"]["images"] == []
