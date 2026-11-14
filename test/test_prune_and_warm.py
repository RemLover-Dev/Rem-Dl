"""Deleted images: dead gallery entries must not linger (manual rescan
prunes them like startup), imports must not re-create them as empty rows,
and the viewer-close call must queue thumb generation."""
import os
import time

import pytest

pytestmark = pytest.mark.usefixtures("bypass_access_guard")
from PIL import Image

import Rems_Dl

H = {"User-Agent": "RemsDlDesktopApp/1.0"}


@pytest.fixture
def client():
    Rems_Dl.app.config["TESTING"] = True
    with Rems_Dl.app.test_client() as c:
        yield c


def _make(master, rel):
    p = os.path.join(str(master), rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    Image.new("RGB", (32, 32)).save(p)
    return os.path.join("Zerochan", "a.png") if rel == "Zerochan/a.png" else rel


def test_prune_dead_entries_keeps_offline_root(tmp_path, monkeypatch):
    monkeypatch.setattr(Rems_Dl, "MASTER_FOLDER", str(tmp_path))
    _make(tmp_path, "Zerochan/a.png")
    key = "deadbeef01"
    gallery = {"images": [
        {"filename": "a.png", "filepath": "Zerochan/a.png"},
        {"filename": "gone.png", "filepath": "Zerochan/gone.png"},
        {"filename": "empty.png", "filepath": ""},
        {"filename": "hdd.png", "filepath": f".gallery_roots/{key}/Danbooru/hdd.png"},
    ]}
    removed_names = Rems_Dl._prune_dead_gallery_entries(gallery)
    names = [i["filename"] for i in gallery["images"]]
    # the real file stays; the deleted file and the empty row go; the
    # offline-root row (link itself missing) is kept
    assert sorted(r["filename"] for r in removed_names) == ["empty.png", "gone.png"]
    assert names == ["a.png", "hdd.png"]


def test_manual_rescan_prunes_dead_entries(client, tmp_path, monkeypatch):
    monkeypatch.setattr(Rems_Dl, "MASTER_FOLDER", str(tmp_path))
    _make(tmp_path, "Zerochan/a.png")
    monkeypatch.setattr(Rems_Dl.DatabaseManager, "load_ui_config",
                        staticmethod(lambda: {"gallery_roots": []}))
    monkeypatch.setattr(Rems_Dl.shared, "load_gallery", lambda: {"images": [
        {"filename": "a.png", "filepath": "Zerochan/a.png"},
        {"filename": "gone.png", "filepath": "Zerochan/gone.png"},
    ]})
    saved = {}
    monkeypatch.setattr(Rems_Dl.shared, "save_gallery", lambda g: saved.update(images=list(g["images"])))
    emitted = []
    monkeypatch.setattr(Rems_Dl.socketio, "emit", lambda e, d=None: emitted.append((e, d)))
    r = client.post("/api/gallery/rescan", headers=H)
    assert r.status_code == 200
    deadline = time.time() + 10
    done = []
    while time.time() < deadline:
        done = [d for e, d in emitted if e == "gallery_rescan_done"]
        if done:
            break
        time.sleep(0.05)
    assert done and done[-1]["removed_entries"] == 1
    assert [i["filename"] for i in saved["images"]] == ["a.png"]
    # the console-log cards for pruned files must be told to vanish too
    deleted = [d for e, d in emitted if e == "image_deleted"]
    assert any(d.get("filename") == "gone.png" for d in deleted)


def test_auto_cull_emits_image_deleted(tmp_path, monkeypatch):
    # TTL-refresh piggyback: rows for externally deleted files vanish on
    # their own (no manual rescan), and each prune announces itself
    monkeypatch.setattr(Rems_Dl, "MASTER_FOLDER", str(tmp_path))
    _make(tmp_path, "Zerochan/a.png")
    # resident gallery: load_gallery returns the SAME dict every call
    gallery = {"images": [
        {"filename": "a.png", "filepath": "Zerochan/a.png"},
        {"filename": "gone.png", "filepath": "Zerochan/gone.png"},
    ]}
    monkeypatch.setattr(Rems_Dl.shared, "load_gallery", lambda: gallery)
    saved = {}
    monkeypatch.setattr(Rems_Dl.shared, "save_gallery", lambda g: saved.update(images=list(g["images"])))
    emitted = []
    monkeypatch.setattr(Rems_Dl.socketio, "emit", lambda e, d=None: emitted.append((e, d)))
    Rems_Dl._auto_cull_gallery()
    assert [i["filename"] for i in saved["images"]] == ["a.png"]
    deleted = [d for e, d in emitted if e == "image_deleted"]
    assert deleted == [{"id": None, "filename": "gone.png",
                        "filepath": "Zerochan/gone.png"}]
    done = [d for e, d in emitted if e == "gallery_rescan_done"]
    assert done and done[-1]["removed_entries"] == 1 and done[-1]["startup"]

    # nothing missing: no save, no events
    emitted.clear()
    saved.clear()
    Rems_Dl._auto_cull_gallery()
    assert not saved and not emitted


def test_import_skips_files_that_are_gone(client, tmp_path, monkeypatch):
    monkeypatch.setattr(Rems_Dl, "MASTER_FOLDER", str(tmp_path))
    _make(tmp_path, "Zerochan/a.png")
    monkeypatch.setattr(Rems_Dl.DatabaseManager, "load_image_history",
                        staticmethod(lambda: [
                            {"site": "zero", "filename": "a.png", "tags": {}, "filepath": "Zerochan/a.png"},
                            {"site": "zero", "filename": "deleted.png", "tags": {}, "filepath": "Zerochan/deleted.png"},
                        ]))
    monkeypatch.setattr(Rems_Dl, "_build_filepath_cache", lambda: {
        "a.png": "Zerochan/a.png",
        "__by_name__": {"a.png": ["Zerochan/a.png"]},
        "__paths__": {"Zerochan/a.png"},
    })
    gallery = {"images": []}
    saved = {}
    monkeypatch.setattr(Rems_Dl.shared, "load_gallery", lambda: gallery)
    monkeypatch.setattr(Rems_Dl.shared, "save_gallery", lambda g: saved.update(images=list(g["images"])))
    r = client.post("/api/gallery/import", headers=H)
    assert r.status_code == 200 and r.get_json()["imported"] == 1
    assert [i["filename"] for i in saved["images"]] == ["a.png"]


def test_warm_thumbs_endpoint(client, tmp_path, monkeypatch):
    cache = tmp_path / "thumb_cache"
    monkeypatch.setattr(Rems_Dl, "MASTER_FOLDER", str(tmp_path))
    monkeypatch.setattr(Rems_Dl, "THUMB_CACHE", str(cache))
    _make(tmp_path, "Zerochan/a.png")
    queued = []
    monkeypatch.setattr(Rems_Dl, "_queue_thumb_warm", lambda full, background=False: queued.append(full))
    r = client.post("/api/gallery/warm_thumbs", json={"filepaths": [
        "Zerochan/a.png",          # valid -> queued
        "Zerochan/missing.png",    # dead  -> skipped
        "../../etc/passwd",        # traversal -> skipped
        "",                        # junk -> skipped
    ]}, headers=H)
    assert r.status_code == 200 and r.get_json()["queued"] == 1
    assert len(queued) == 1 and queued[0].endswith(os.path.join("Zerochan", "a.png"))
