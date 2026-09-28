import json

from core import shared


def _write(cache_dir, site, data):
    (cache_dir / f"{site}_tags.json").write_text(json.dumps(data), encoding="utf-8")


def test_v1_cache_drops_unverified_general(tmp_path, monkeypatch):
    monkeypatch.setattr(shared, "TAG_CACHE_DIR", str(tmp_path))
    _write(tmp_path, "gelbooru", {
        "some_character": "character",
        "poisoned_failure": "tag",
        "real_general": "tag",
        "safebooru_style": 0,
    })
    data = shared.load_tag_cache("gelbooru")
    # real categories survive; unverified general entries are re-queued
    assert data["some_character"] == "character"
    assert "poisoned_failure" not in data
    assert "real_general" not in data
    assert "safebooru_style" not in data
    assert data["_v"] == 2


def test_v2_cache_keeps_everything(tmp_path, monkeypatch):
    monkeypatch.setattr(shared, "TAG_CACHE_DIR", str(tmp_path))
    _write(tmp_path, "gelbooru", {
        "_v": 2,
        "general_ok": "tag",
        "int_style": 0,
        "character": "character",
    })
    data = shared.load_tag_cache("gelbooru")
    assert data["general_ok"] == "tag"
    assert data["int_style"] == 0
    assert data["character"] == "character"


def test_missing_or_broken_cache_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(shared, "TAG_CACHE_DIR", str(tmp_path))
    assert shared.load_tag_cache("absent") == {}
    (tmp_path / "absent_tags.json").write_text("not json", encoding="utf-8")
    assert shared.load_tag_cache("absent") == {}


def test_round_trip_persists_version(tmp_path, monkeypatch):
    monkeypatch.setattr(shared, "TAG_CACHE_DIR", str(tmp_path))
    _write(tmp_path, "yande", {"old": 0, "char": 4})
    shared.save_tag_cache(shared.load_tag_cache("yande"), "yande")
    data = shared.load_tag_cache("yande")
    assert data["_v"] == 2
    assert data["char"] == 4
    assert "old" not in data
