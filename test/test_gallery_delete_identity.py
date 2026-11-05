"""Flask-route regressions for exact file deletion in a multi-root gallery."""
import os

import pytest

pytest.importorskip("flask")
pytest.importorskip("flask_socketio")
pytest.importorskip("curl_cffi")
try:
    import Rems_Dl
except ImportError as exc:  # optional adapter/runtime dependency in lean test images
    pytest.skip(f"Rems_Dl runtime dependencies are unavailable: {exc}", allow_module_level=True)

from core import gallery_store, shared
from core.database import DatabaseManager

pytestmark = pytest.mark.usefixtures("bypass_access_guard")


@pytest.fixture
def gallery_app_env(tmp_path, monkeypatch):
    root = tmp_path / "master"
    root.mkdir()
    monkeypatch.setattr(Rems_Dl, "MASTER_FOLDER", str(root))
    monkeypatch.setattr(shared, "MASTER_FOLDER", str(root))
    monkeypatch.setattr(Rems_Dl, "_allowed_image_dir", lambda _path: True)
    monkeypatch.setattr(Rems_Dl, "THUMB_CACHE", str(tmp_path / "thumbs"))
    monkeypatch.setattr(Rems_Dl, "DISPLAY_CACHE_DIR", str(tmp_path / "display"))
    monkeypatch.setattr(Rems_Dl, "PREVIEW_CACHE_DIR", str(tmp_path / "preview"))
    for directory in (Rems_Dl.THUMB_CACHE, Rems_Dl.DISPLAY_CACHE_DIR, Rems_Dl.PREVIEW_CACHE_DIR):
        os.makedirs(directory, exist_ok=True)
    gallery_store.use_store_path(str(tmp_path / "gallery.sqlite3"))
    monkeypatch.setattr(shared, "_gallery_cache", {
        "data": {"images": []}, "filenames": set(), "by_fn": {},
        "by_path": {}, "by_fn_paths": {},
    })
    events = []
    monkeypatch.setattr(Rems_Dl.socketio, "emit", lambda name, payload: events.append((name, payload)))
    Rems_Dl.app.config["TESTING"] = True
    with Rems_Dl.app.test_client() as client:
        yield root, client, events
    gallery_store.use_store_path(None)


def _add_same_name_pair(root):
    first = root / "Danbooru" / "tag-a" / "same.jpg"
    second = root / "Pixiv" / "tag-b" / "same.jpg"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_bytes(b"first image")
    second.write_bytes(b"second image")
    shared.add_to_gallery("danbooru", "same.jpg", "Danbooru/tag-a/same.jpg", ["first"], [])
    shared.add_to_gallery("pixiv", "same.jpg", "Pixiv/tag-b/same.jpg", ["second"], [])
    DatabaseManager.add_image_history("danbooru", "same.jpg", ["first"], [],
                                      filepath="Danbooru/tag-a/same.jpg")
    DatabaseManager.add_image_history("pixiv", "same.jpg", ["second"], [],
                                      filepath="Pixiv/tag-b/same.jpg")
    return first, second


def test_delete_by_filepath_removes_only_matching_gallery_and_history(gallery_app_env):
    root, client, events = gallery_app_env
    first, second = _add_same_name_pair(root)

    response = client.post("/api/gallery/delete_by_name", json={
        "filename": "same.jpg", "filepath": "Danbooru/tag-a/same.jpg",
    })
    assert response.status_code == 200
    assert not first.exists()
    assert second.exists()
    rows = shared.load_gallery()["images"]
    assert [(row["filename"], row["filepath"]) for row in rows] == [
        ("same.jpg", "Pixiv/tag-b/same.jpg")
    ]
    history = DatabaseManager.load_image_history()
    assert [row["filepath"] for row in history] == ["Pixiv/tag-b/same.jpg"]
    deleted = [payload for name, payload in events if name == "image_deleted"]
    assert len(deleted) == 1
    assert deleted[0]["filepath"] == "Danbooru/tag-a/same.jpg"
    assert deleted[0]["id"] != rows[0]["id"]


def test_ambiguous_filename_delete_is_rejected(gallery_app_env):
    root, client, _events = gallery_app_env
    _add_same_name_pair(root)
    response = client.post("/api/gallery/delete_by_name", json={"filename": "same.jpg"})
    assert response.status_code == 409
    assert len(shared.load_gallery()["images"]) == 2


def test_permission_error_does_not_remove_gallery_or_history_rows(gallery_app_env, monkeypatch):
    root, client, _events = gallery_app_env
    first, second = _add_same_name_pair(root)
    real_remove = os.remove

    def fail_target(path, *args, **kwargs):
        if os.path.realpath(path) == os.path.realpath(first):
            raise PermissionError("simulated access denied")
        return real_remove(path, *args, **kwargs)

    monkeypatch.setattr(Rems_Dl.os, "remove", fail_target)
    response = client.post("/api/gallery/delete_by_name", json={
        "filename": "same.jpg", "filepath": "Danbooru/tag-a/same.jpg",
    })
    assert response.status_code == 500
    assert first.exists() and second.exists()
    assert len(shared.load_gallery()["images"]) == 2
    assert len(DatabaseManager.load_image_history()) == 2


def test_keep_larger_cleanup_removes_exact_gallery_history_and_versioned_caches(gallery_app_env):
    root, _client, events = gallery_app_env
    first, second = _add_same_name_pair(root)
    first_stat = os.stat(first)
    stat_info = (first_stat.st_mtime_ns, first_stat.st_size)

    # Seed every cache family using the same stat-derived key as the reader.
    from core import preview_cache
    cache_paths = [
        Rems_Dl._thumb_cache_path(str(first), ".jpg"),
        Rems_Dl._thumb_cache_path(str(first), ".png"),
        Rems_Dl._thumb_cache_path(str(first), ".json"),
        Rems_Dl._display_cache_path_raw(str(first), ".jpg", stat_info),
        Rems_Dl._display_cache_path_raw(str(first), ".png", stat_info),
        preview_cache._cache_path(str(first), Rems_Dl.PREVIEW_CACHE_DIR, stat_info),
    ]
    for path in cache_paths:
        with open(path, "wb") as stream:
            stream.write(b"cached")

    os.remove(first)
    Rems_Dl._dedup_replaced_cleanup(str(first), stat_info)

    rows = shared.load_gallery()["images"]
    assert [row["filepath"] for row in rows] == ["Pixiv/tag-b/same.jpg"]
    assert [row["filepath"] for row in DatabaseManager.load_image_history()] == [
        "Pixiv/tag-b/same.jpg"
    ]
    assert all(not os.path.exists(path) for path in cache_paths)
    deleted = [payload for name, payload in events if name == "image_deleted"]
    assert len(deleted) == 1
    assert deleted[0]["filepath"] == "Danbooru/tag-a/same.jpg"


def test_keep_larger_cleanup_without_gallery_row_does_not_emit_delete(gallery_app_env):
    root, _client, events = gallery_app_env
    source = root / "Danbooru" / "tag-a" / "orphan.jpg"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"orphan")
    DatabaseManager.add_image_history("danbooru", "orphan.jpg", [], [],
                                      filepath="Danbooru/tag-a/orphan.jpg")
    os.remove(source)
    Rems_Dl._dedup_replaced_cleanup(str(source), None)
    assert not [payload for name, payload in events if name == "image_deleted"]
    assert DatabaseManager.load_image_history() == []


def test_history_favourite_endpoint_accepts_filepath_without_filename(gallery_app_env):
    root, client, _events = gallery_app_env
    _add_same_name_pair(root)
    response = client.post("/api/gallery/favourite_by_name", json={
        "filepath": "Pixiv/tag-b/same.jpg",
    })
    assert response.status_code == 200
    rows = {row["filepath"]: row for row in shared.load_gallery()["images"]}
    assert rows["Pixiv/tag-b/same.jpg"]["favourite"] is True
    assert rows["Danbooru/tag-a/same.jpg"]["favourite"] is False
