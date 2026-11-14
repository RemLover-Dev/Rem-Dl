"""Regression tests for the review-fix batch:
1. api-settings secrets are masked on GET and sentinel-preserved on POST
2. gallery_file serves planted unsafe files (html/svg) as attachment
3. a rescan running while a download appends (or an auto-cull rebinds the
   list) must not lose either side's rows
"""
import threading

import pytest

pytestmark = pytest.mark.usefixtures("bypass_access_guard")

import Rems_Dl
import core.shared as shared
from core import gallery_store

FOLDER = "someartist_(x)"


@pytest.fixture
def lib(tmp_path, monkeypatch):
    (tmp_path / "gelbooru" / FOLDER).mkdir(parents=True)
    monkeypatch.setattr(Rems_Dl, "MASTER_FOLDER", str(tmp_path))
    monkeypatch.setattr(shared, "_gallery_cache",
                        {"data": {"images": []}, "filenames": set(), "by_fn": {}})
    gallery_store.use_store_path(str(tmp_path / "gallery.db"))
    yield tmp_path
    gallery_store.use_store_path(None)


class StubSettings:
    def __init__(self, data):
        self.data = dict(data)

    def load_api_settings(self):
        return dict(self.data)

    def save_api_settings(self, d):
        self.data.update(d)


@pytest.fixture
def client():
    Rems_Dl.app.config["TESTING"] = True
    with Rems_Dl.app.test_client() as c:
        yield c


def test_api_settings_masks_secrets_and_preserves_them_on_roundtrip(lib, client, monkeypatch):
    stub = StubSettings({
        "gelbooru_api_key": "abcd1234",
        "pixiv_refresh_token": "tok1234",
        "konachan_password": "",
        "pinterest_cookies": "cookies.json",
        "x_username": "rem",
    })
    monkeypatch.setattr(Rems_Dl, "settings", stub)

    got = client.get("/api/api-settings").get_json()
    assert got["gelbooru_api_key"] == "\u2022\u2022\u2022\u20221234"
    assert got["pixiv_refresh_token"] == "\u2022\u2022\u2022\u20221234"
    assert got["konachang_password" if False else "konachan_password"] == ""
    # file PATHS and logins stay readable — the frontend shows them
    assert got["pinterest_cookies"] == "cookies.json"
    assert got["x_username"] == "rem"
    assert "abcd1234" not in (got.get("gelbooru_api_key") or "")

    # the settings UI saves back everything it was given: masked sentinels
    # must be replaced by the stored value, not saved as the mask
    resp = client.post("/api/api-settings", json=got)
    assert resp.status_code == 200
    assert stub.data["gelbooru_api_key"] == "abcd1234"
    assert stub.data["pixiv_refresh_token"] == "tok1234"

    # a genuinely typed new value still replaces the stored one
    got["gelbooru_api_key"] = "newkey"
    client.post("/api/api-settings", json=got)
    assert stub.data["gelbooru_api_key"] == "newkey"


def test_gallery_file_downloads_planted_html_inline_png(lib, client):
    import os
    png = lib / "gelbooru" / FOLDER / "safe.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")
    html = lib / "gelbooru" / FOLDER / "evil.html"
    html.write_text("<script>alert(1)</script>")

    r_png = client.get(f"/api/gallery/file/gelbooru/{FOLDER}/safe.png")
    assert r_png.status_code == 200
    assert "attachment" not in r_png.headers.get("Content-Disposition", "")
    # a disguised payload must never be content-sniffed into HTML/JS
    assert r_png.headers.get("X-Content-Type-Options") == "nosniff"

    r_html = client.get(f"/api/gallery/file/gelbooru/{FOLDER}/evil.html")
    # non-media files never leave the library — 403, no body, no download
    assert r_html.status_code == 403
    assert b"alert" not in r_html.data

    # traversal dies at safe_join before any os call sees it
    r_esc = client.get("/api/gallery/file/%2e%2e%2f%2e%2e%2fdatabase%2fgallery.db")
    assert r_esc.status_code == 403
    r_esc = client.get("/api/gallery/file/gelbooru/%2e%2e%2f%2e%2e%2fdatabase%2fgallery.db")
    assert r_esc.status_code == 403


def _jpg(lib, name, tags, artists):
    p = lib / "gelbooru" / FOLDER / name
    p.write_bytes(b"\xff\xd8\xff\xd9")
    shared.write_image_metadata(str(p), tags, artists, "gelbooru")
    return p


def test_completion_log_waits_for_held_publishes():
    """The 'All N downloads completed' line and the success bar must land
    AFTER the batch's held [SUCCESS] cards, not before them (JXL hold defers
    image cards); while draining, conversion_progress events flow."""
    import core.shared as shared
    import time as time_mod

    emitted = []
    events = []
    real_emit = shared.emit_callback
    shared.emit_callback = lambda ev, data: events.append((ev, data)) if ev != "python_log" else None
    try:
        held = {"fp": {"site": "gelbooru", "filepath": "/nonexistent/x.png",
                       "filename": "x.png", "held": time_mod.time()}}
        with shared._PUBLISH_HOLD_LOCK:
            shared._PENDING_PUBLISH["fp"] = held["fp"]

        shared.finish_report("gelbooru", 3, 0, 0, False, emitted.append)
        assert not emitted, "completion line logged while publishes still held"
        assert ("conversion_progress", {"worker": "gelbooru", "done": 0, "total": 1, "current": None}) in events

        # a conversion releasing mid-window emits progress toward the total
        shared.notify_convert_start("fp")
        assert any(ev == "conversion_progress" and d.get("current") == "x.png"
                   for ev, d in events), "convert-start tick missing"
        spec = shared._pop_pending_publish("fp")
        assert spec is not None
        assert ("conversion_progress", {"worker": "gelbooru", "done": 1, "total": 1}) in events

        deadline = time_mod.time() + 10
        while not emitted and time_mod.time() < deadline:
            time_mod.sleep(0.1)
        assert len(emitted) == 1 and "downloads completed successfully" in emitted[0]
        assert any(ev == "worker_finished" and d.get("worker") == "gelbooru"
                   for ev, d in events), "success bar never re-emitted after drain"
        assert "gelbooru" not in shared._CONVERT_STATE

        # no pending publishes: immediate, no thread
        events.clear()
        emitted2 = []
        shared.finish_report("gelbooru", 1, 0, 0, False, emitted2.append)
        assert len(emitted2) == 1
        assert not any(ev == "conversion_progress" for ev, _ in events)
    finally:
        shared.emit_callback = real_emit


def test_image_history_paging_and_search(lib, client):
    """/api/image_history?limit=&offset=&q= serves the archive in pages with
    X-Total-Count; no params keeps the legacy full-list shape."""
    shared._store().history_insert({"site": "gelbooru", "filename": "a.jpg",
                                    "filepath": "gelbooru/x/a.jpg",
                                    "tags": {"tag": ["blue_hair"]},
                                    "downloaded_at": 100})
    shared._store().history_insert({"site": "dan", "filename": "b.png",
                                    "filepath": "dan/y/b.png",
                                    "tags": {"tag": ["red_eyes"]},
                                    "downloaded_at": 200})
    shared._store().history_insert({"site": "dan", "filename": "c.png",
                                    "filepath": "dan/y/c.png",
                                    "tags": {"tag": ["smile"]},
                                    "downloaded_at": 300})

    full = client.get("/api/image_history")
    rows = full.get_json()
    assert len(rows) == 3 and rows[0]["filename"] == "c.png"  # newest first

    p1 = client.get("/api/image_history?limit=2")
    assert len(p1.get_json()) == 2
    assert p1.headers["X-Total-Count"] == "3"
    p2 = client.get("/api/image_history?limit=2&offset=2")
    assert [r["filename"] for r in p2.get_json()] == ["a.jpg"]

    q = client.get("/api/image_history?limit=10&q=red_eyes")
    assert [r["filename"] for r in q.get_json()] == ["b.png"]
    assert q.headers["X-Total-Count"] == "1"

    rng = client.get("/api/image_history?limit=10&from_ts=250")
    assert [r["filename"] for r in rng.get_json()] == ["c.png"]


def test_rescan_keeps_rows_added_during_the_walk(lib):
    """add_to_gallery racing a live scan: the download row must survive."""
    _jpg(lib, "a.jpg", ["blue_hair"], [FOLDER])
    _jpg(lib, "b.jpg", ["red_eyes"], [FOLDER])

    real_tags = Rems_Dl._tags_from_file
    started = threading.Event()
    added = threading.Event()

    def hooked(full):
        out = real_tags(full)
        if not started.is_set():
            started.set()
            assert added.wait(timeout=10), "add never happened during the walk"
        return out

    Rems_Dl._tags_from_file = hooked
    try:
        scan_exc = []

        def run_scan():
            try:
                Rems_Dl._scan_and_merge_gallery()
            except Exception as e:
                scan_exc.append(e)

        t = threading.Thread(target=run_scan)
        t.start()
        assert started.wait(timeout=10)
        shared.add_to_gallery("gelbooru", "new.jpg", f"gelbooru/{FOLDER}/new.jpg",
                              ["hug"], [FOLDER], characters=["rem"])
        added.set()
        t.join(timeout=30)
        assert not t.is_alive(), "scan hung"
        assert not scan_exc, scan_exc
    finally:
        Rems_Dl._tags_from_file = real_tags

    images = shared.load_gallery()["images"]
    names = [i.get("filename") for i in images]
    for fn in ("a.jpg", "b.jpg", "new.jpg"):
        assert names.count(fn) == 1, (fn, names)
    new_row = next(i for i in images if i.get("filename") == "new.jpg")
    tags = shared.entry_tags(new_row)
    assert tags["tag"] == ["hug"]
    assert tags["character"] == ["rem"]


def test_rescan_reattaches_rows_after_a_cull_rebind(lib):
    """auto-cull (prune + gallery['images'] = kept) mid-walk must not swallow
    the scan's own appends; the ghost row must still be gone."""
    _jpg(lib, "a.jpg", ["blue_hair"], [FOLDER])
    _jpg(lib, "b.jpg", ["red_eyes"], [FOLDER])
    shared.load_gallery()["images"].append({
        "id": "ghost", "filename": "ghost.jpg",
        "filepath": f"gelbooru/{FOLDER}/ghost.jpg",
        "site": "gelbooru", "favourite": False,
        "downloaded_at": "2026-10-01T00:00:00"})
    shared._gallery_cache["filenames"].add("ghost.jpg")

    real_tags = Rems_Dl._tags_from_file
    pruned = threading.Event()

    def hooked(full):
        out = real_tags(full)
        if not pruned.is_set():
            # exact auto-cull body, run under the lock like _auto_cull_gallery
            with shared._GALLERY_LOCK:
                gallery = shared.load_gallery()
                removed = Rems_Dl._prune_dead_gallery_entries(gallery)
                if removed:
                    shared.save_gallery(gallery)
            pruned.set()
        return out

    Rems_Dl._tags_from_file = hooked
    try:
        scan_exc = []

        def run_scan():
            try:
                Rems_Dl._scan_and_merge_gallery()
            except Exception as e:
                scan_exc.append(e)

        t = threading.Thread(target=run_scan)
        t.start()
        t.join(timeout=30)
        assert not t.is_alive(), "scan hung"
        assert not scan_exc, scan_exc
    finally:
        Rems_Dl._tags_from_file = real_tags

    images = shared.load_gallery()["images"]
    names = [i.get("filename") for i in images]
    for fn in ("a.jpg", "b.jpg"):
        assert names.count(fn) == 1, (fn, names)
    assert "ghost.jpg" not in names
