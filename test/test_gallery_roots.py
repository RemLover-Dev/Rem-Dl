"""Multi-root gallery scan: extra folders (HDD) join the library and
moved/deleted images keep their gallery + hash records."""
import os

import pytest
from PIL import Image

import Rems_Dl

H = {"User-Agent": "RemsDlDesktopApp/1.0"}


@pytest.fixture
def client():
    Rems_Dl.app.config["TESTING"] = True
    with Rems_Dl.app.test_client() as c:
        yield c


@pytest.fixture
def roots_env(tmp_path, monkeypatch):
    master = tmp_path / "ssd"
    hdd_root = tmp_path / "hdd"
    (master / "Zerochan").mkdir(parents=True)
    (hdd_root / "Danbooru").mkdir(parents=True)
    Image.new("RGB", (64, 64), color=(30, 60, 90)).save(master / "Zerochan" / "a.png")
    Image.new("RGB", (64, 64), color=(10, 200, 40)).save(hdd_root / "Danbooru" / "b.png")
    monkeypatch.setattr(Rems_Dl, "MASTER_FOLDER", str(master))
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    roots_added = []
    monkeypatch.setattr(Rems_Dl.DatabaseManager, "load_ui_config",
                        staticmethod(lambda: {"gallery_roots": list(roots_added)}))

    def fake_update(changes):
        if "gallery_roots" in changes:
            roots_added[:] = changes["gallery_roots"]
        return {"gallery_roots": list(roots_added)}
    monkeypatch.setattr(Rems_Dl.DatabaseManager, "update_ui_config",
                        staticmethod(fake_update))
    return Rems_Dl, master, hdd_root, roots_added


def test_extra_root_indexed_and_routes_allowed(client, roots_env, monkeypatch):
    Rems_Dl, master, hdd_root, roots = roots_env
    r = client.post("/api/gallery/roots", json={"path": str(hdd_root)}, headers=H)
    assert r.status_code == 200 and r.get_json()["roots"] == [str(hdd_root)]
    assert roots == [str(hdd_root)]
    monkeypatch.setattr(Rems_Dl.shared, "load_gallery", lambda: {"images": []})

    gallery, added, _fixed, _checked = Rems_Dl._scan_and_merge_gallery()
    paths = {i["filename"]: i["filepath"] for i in gallery["images"]}
    assert added == 2
    # master files stay relative to master, HDD files resolve through the
    # hidden .gallery_roots symlink (relative => URLs keep working)
    key = Rems_Dl._root_key(os.path.realpath(str(hdd_root)))
    assert paths["a.png"] == os.path.join("Zerochan", "a.png")
    assert paths["b.png"] == os.path.join(".gallery_roots", key, "Danbooru", "b.png")
    # the symlink exists and points at the real folder
    link = os.path.join(str(master), ".gallery_roots", key)
    assert os.path.islink(link) and os.path.realpath(link) == os.path.realpath(str(hdd_root))
    assert os.path.isfile(os.path.join(str(master), paths["b.png"]))

    # extra-root image is fetchable through the file route under the guard
    r = client.get(f"/api/gallery/file/{paths['b.png']}", headers=H)
    assert r.status_code == 200
    # traversal into an unrelated folder stays forbidden
    r = client.get("/api/gallery/file/../../etc/passwd", headers=H)
    assert r.status_code == 403

    # removing the root drops the symlink: the route denies the HDD path
    r = client.delete("/api/gallery/roots", json={"path": str(hdd_root)}, headers=H)
    assert r.status_code == 200
    assert not os.path.islink(link)
    r = client.get(f"/api/gallery/file/{paths['b.png']}", headers=H)
    assert r.status_code == 404


def test_clipboard_accepts_extra_root_paths(client, roots_env):
    """Copy-to-clipboard sends .gallery_roots/<key>/... URIs: they must pass
    the path guard like master files (403 here made the UI paste the bitmap
    instead of the file URI). No clipboard tool on the test box -> 501 is the
    success-shape; only the guard verdict matters."""
    Rems_Dl, _master, hdd_root, _roots = roots_env
    r = client.post("/api/gallery/roots", json={"path": str(hdd_root)}, headers=H)
    assert r.status_code == 200
    key = Rems_Dl._root_key(os.path.realpath(str(hdd_root)))
    rel = f".gallery_roots/{key}/Danbooru/b.png"
    # a clipboard tool on the box may fail without a display (500/501): only
    # the path-guard verdict matters — 403/404 mean the guard still blocks
    r = client.post("/api/clipboard?uri=1", data=rel.encode(), headers=H)
    assert r.status_code not in (403, 404), r.get_data(as_text=True)
    r = client.post("/api/clipboard?uri=1", data=b"Zerochan/a.png", headers=H)
    assert r.status_code not in (403, 404)
    r = client.post("/api/clipboard?uri=1", data=b"../../etc/passwd", headers=H)
    assert r.status_code == 403


def test_moved_image_adopted_from_extra_root(client, roots_env, monkeypatch):
    Rems_Dl, master, hdd_root, roots_added = roots_env
    Image.new("RGB", (64, 64), color=(1, 2, 3)).save(master / "Zerochan" / "moved.png")
    roots_added.append(str(hdd_root))
    gal = {"images": [{"id": "x", "filename": "moved.png",
                       "filepath": "Zerochan/moved.png", "site": "Zerochan",
                       "tags": {"tag": ["original tags"]},
                       "downloaded_at": "2026-01-01T00:00:00"}]}
    monkeypatch.setattr(Rems_Dl.shared, "load_gallery", lambda: gal)
    # the SSD copy is gone: moved to the HDD
    (master / "Zerochan" / "moved.png").unlink()
    Image.new("RGB", (64, 64), color=(1, 2, 3)).save(hdd_root / "Danbooru" / "moved.png")

    gallery, added, _fixed, _checked = Rems_Dl._scan_and_merge_gallery()
    row = {i["filename"]: i for i in gallery["images"]}
    # a.png (master) + b.png (hdd) are genuinely new; moved.png is adopted
    assert added == 2
    assert len(gallery["images"]) == 3
    key = Rems_Dl._root_key(os.path.realpath(str(hdd_root)))
    assert row["moved.png"]["filepath"] == os.path.join(
        ".gallery_roots", key, "Danbooru", "moved.png")
    assert row["moved.png"]["tags"] == {"tag": ["original tags"]}  # identity kept


def test_filenames_do_not_shadow_between_roots(client, roots_env, monkeypatch):
    Rems_Dl, master, hdd_root, roots_added = roots_env
    # SAME filename on both folders: master copy exists, so it wins and the
    # HDD twin is NOT adopted as a move
    Image.new("RGB", (64, 64), color=(200, 10, 10)).save(master / "Zerochan" / "twin.png")
    Image.new("RGB", (64, 64), color=(5, 5, 200)).save(hdd_root / "Danbooru" / "twin.png")
    roots_added.append(str(hdd_root))
    gal = {"images": [{"id": "t", "filename": "twin.png",
                       "filepath": "Zerochan/twin.png", "site": "Zerochan",
                       "tags": {}, "downloaded_at": "2026-01-01T00:00:00"}]}
    monkeypatch.setattr(Rems_Dl.shared, "load_gallery", lambda: gal)
    gallery, added, _fixed, _checked = Rems_Dl._scan_and_merge_gallery()
    row = {i["filename"]: i for i in gallery["images"]}
    # a.png + b.png from the fixture are new; twin.png is NOT adopted as a move
    assert added == 2
    assert len(gallery["images"]) == 3
    assert row["twin.png"]["filepath"] == "Zerochan/twin.png"  # master stays


def test_startup_rescan_keeps_rows_of_offline_roots(client, roots_env, monkeypatch):
    Rems_Dl, _master, _hdd_root, roots_added = roots_env
    # root path is configured but "unmounted" (does not exist): the roots
    # list drops it, no link is healed, and stored .gallery_roots rows must
    # be KEPT instead of pruned as dead
    ghost = "/mnt/definitely_not_a_mounted_rems_test_root"
    roots_added.append(ghost)
    key = Rems_Dl._root_key(ghost)
    gal = {"images": [
        {"id": "1", "filename": "a.png", "filepath": os.path.join("Zerochan", "a.png"),
         "site": "Zerochan", "tags": {}, "downloaded_at": ""},
        {"id": "2", "filename": "b.png",
         "filepath": os.path.join(".gallery_roots", key, "Danbooru", "b.png"),
         "site": "Danbooru", "tags": {}, "downloaded_at": ""},
        {"id": "3", "filename": "gone.png", "filepath": os.path.join("Zerochan", "gone.png"),
         "site": "Zerochan", "tags": {}, "downloaded_at": ""},
    ]}
    monkeypatch.setattr(Rems_Dl.shared, "load_gallery", lambda: gal)
    saved = []
    monkeypatch.setattr(Rems_Dl.shared, "save_gallery", lambda g: saved.append(g))
    Rems_Dl.startup_rescan()
    assert saved, "startup rescan must persist"
    rows = {i["filename"]: i for i in saved[0]["images"]}
    assert "a.png" in rows          # live master file
    assert "b.png" in rows          # offline-root row: KEPT, not pruned
    assert "gone.png" not in rows   # genuinely dead master row: pruned


def test_slash_merged_abs_url_recovers(client, roots_env, monkeypatch):
    # the frontend builds "/api/gallery/file//mnt/hdd/..." for absolute rows;
    # werkzeug's slash-merge 308s to "/api/gallery/file/mnt/hdd/..." (leading
    # slash eaten) — the route must still serve the real file
    Rems_Dl, _master, hdd_root, _roots = roots_env
    client.post("/api/gallery/roots", json={"path": str(hdd_root)}, headers=H)
    monkeypatch.setattr(Rems_Dl.shared, "load_gallery", lambda: {"images": []})
    Rems_Dl._scan_and_merge_gallery()
    merged = f"/api/gallery/file{os.path.realpath(str(hdd_root))}/Danbooru/b.png".replace(
        "//", "/")           # exact post-308 shape: relative-looking abs path
    r = client.get(merged, headers=H)
    assert r.status_code == 200
    # and the thumbnails/display routes recover the same way
    r2 = client.get(f"/api/gallery/thumb{merged[len('/api/gallery/file'):]}", headers=H)
    assert r2.status_code == 200
