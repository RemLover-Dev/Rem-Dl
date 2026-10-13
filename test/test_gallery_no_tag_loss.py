"""The tag-loss bug must not come back.

gallery.json lost records (batched write + hard exit) and the folder rescan
rebuilt them carrying only the folder name — so a download's tags were gone.
These pin down the three fixes: the scan recovers the tags the file itself
carries, the download's tags beat a scan placeholder, and every exit path
flushes the batched writes.
"""
import os

import pytest

# Rems_Dl's load_dotenv() pollutes os.environ, which SettingsManager reads for
# its defaults — snapshot and restore so test_settings stays isolated (same
# dance as test_gallery_select, needed because this file imports Rems_Dl first).
_ENV_KEYS = [
    "API_TIMEOUT", "RETRY_WAIT", "ANTI_BAN_PAUSE", "DOWNLOAD_RETRIES",
    "USE_PROXY", "PROXY_URL", "VERIFY_TLS",
]
_env_before = {k: os.environ.get(k) for k in _ENV_KEYS}

import Rems_Dl  # noqa: E402

for _k, _v in _env_before.items():
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v

from core import gallery_store, shared  # noqa: E402

FOLDER = "someartist_(x)"


@pytest.fixture
def lib(tmp_path, monkeypatch):
    (tmp_path / "gelbooru" / FOLDER).mkdir(parents=True)
    monkeypatch.setattr(Rems_Dl, "MASTER_FOLDER", str(tmp_path))
    monkeypatch.setattr(shared, "_gallery_cache",
                        {"data": {"images": []}, "filenames": set(), "dirty": 0})
    # per-test store: these tests insert real rows, and a shared one would
    # let one test's tags answer another test's lookup
    gallery_store.use_store_path(str(tmp_path / "gallery.db"))
    yield tmp_path
    gallery_store.use_store_path(None)


def _jpg(lib, name, tags, artists, copyright_=None):
    """A file whose embedded block carries its real tags."""
    p = lib / "gelbooru" / FOLDER / name
    p.write_bytes(b"\xff\xd8\xff\xd9")
    shared.write_image_metadata(str(p), tags, artists, "gelbooru",
                                copyrights=[copyright_] if copyright_ else None)
    return p


def test_rescan_reads_tags_from_the_file_not_the_folder(lib):
    _jpg(lib, "abcdef.jpg", ["blue_hair"], [FOLDER], copyright_="touhou")

    gallery, added, _fixed, _checked = Rems_Dl._scan_and_merge_gallery()

    assert added == 1
    entry = gallery["images"][0]
    assert entry["tags"]["artist"] == [FOLDER]
    assert entry["tags"]["tag"] == ["blue_hair"]
    assert entry["tags"]["copyright"] == ["touhou"]


def test_rescan_restores_a_folder_only_entry_from_the_file(lib):
    rel = f"gelbooru/{FOLDER}/abcdef.jpg"
    _jpg(lib, "abcdef.jpg", ["blue_hair"], [FOLDER])
    # what an earlier rebuild left behind: the only tag is the folder
    shared.load_gallery()["images"].append({
        "id": "x", "filename": "abcdef.jpg", "filepath": rel, "site": "gelbooru",
        "tags": {"tag": [], "artist": [FOLDER]}, "favourite": False,
        "downloaded_at": "2026-08-19T00:00:00"})
    shared._gallery_cache["filenames"].add("abcdef.jpg")

    gallery, added, fixed, _checked = Rems_Dl._scan_and_merge_gallery()

    assert added == 0 and fixed >= 1
    assert gallery["images"][0]["tags"]["tag"] == ["blue_hair"]


def test_rescan_never_overwrites_tags_it_already_has(lib):
    _jpg(lib, "abcdef.jpg", ["file_says_this"], ["file_artist"])
    shared.load_gallery()["images"].append({
        "id": "x", "filename": "abcdef.jpg",
        "filepath": f"gelbooru/{FOLDER}/abcdef.jpg", "site": "gelbooru",
        "tags": {"artist": [FOLDER], "character": ["rem"], "tag": ["1girl", "smile"]},
        "favourite": False, "downloaded_at": "2026-09-01T00:00:00"})
    shared._gallery_cache["filenames"].add("abcdef.jpg")

    gallery, _added, fixed, _checked = Rems_Dl._scan_and_merge_gallery()

    assert gallery["images"][0]["tags"]["tag"] == ["1girl", "smile"]
    assert gallery["images"][0]["tags"]["character"] == ["rem"]
    assert fixed == 0


def test_rescan_falls_back_to_the_folder_when_the_file_carries_nothing(lib):
    (lib / "gelbooru" / FOLDER / "plain.jpg").write_bytes(b"\xff\xd8\xff\xd9")

    gallery, _added, _fixed, _checked = Rems_Dl._scan_and_merge_gallery()

    assert gallery["images"][0]["tags"] == {"tag": [FOLDER]}


def test_a_file_without_tags_is_read_at_most_once(lib, monkeypatch):
    """Bare files are gigabytes; scanning them once per launch is not a scan."""
    (lib / "gelbooru" / FOLDER / "plain.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    reads = []
    real = Rems_Dl._tags_from_file
    monkeypatch.setattr(Rems_Dl, "_tags_from_file",
                        lambda full: (reads.append(full), real(full))[1])

    gallery, added, _fixed, _checked = Rems_Dl._scan_and_merge_gallery()
    assert added == 1 and len(reads) == 1

    _gallery, added, _fixed, _checked = Rems_Dl._scan_and_merge_gallery()
    assert added == 0
    assert len(reads) == 1          # unchanged file, not read again


def test_duplicate_keeps_the_twin_that_has_the_tags(lib):
    rel = f"gelbooru/{FOLDER}/abcdef.jpg"
    _jpg(lib, "abcdef.jpg", ["blue_hair"], [FOLDER])
    # scan-time rebuild landed first, the tagged download second
    shared.load_gallery()["images"] = [
        {"id": "a", "filename": "abcdef.jpg", "filepath": "", "site": "gelbooru",
         "tags": {"tag": [], "artist": [FOLDER]}, "favourite": False,
         "downloaded_at": "2026-08-19T00:00:00"},
        {"id": "b", "filename": "abcdef.jpg", "filepath": rel, "site": "gelbooru",
         "tags": {"artist": [FOLDER], "tag": ["blue_hair", "1girl"]},
         "favourite": True, "downloaded_at": "2026-10-01T00:00:00"},
    ]

    gallery, _added, _fixed, _checked = Rems_Dl._scan_and_merge_gallery()

    assert len(gallery["images"]) == 1
    entry = gallery["images"][0]
    assert entry["tags"]["tag"] == ["blue_hair", "1girl"]
    assert entry["filepath"] != ""
    assert entry["favourite"] is True


def test_download_tags_beat_a_scan_placeholder(lib):
    """A scan that saw the file land first must not swallow the download."""
    fn = "abcdef.jpg"
    shared.load_gallery()["images"].append({
        "id": "x", "filename": fn, "filepath": f"gelbooru/{FOLDER}/{fn}",
        "site": "gelbooru", "tags": {"tag": [FOLDER]}, "favourite": False,
        "downloaded_at": "2026-10-01T00:00:00"})
    shared._gallery_cache["filenames"].add(fn)

    shared.add_to_gallery("gelbooru", fn, f"gelbooru/{FOLDER}/{fn}",
                          ["blue_hair", "1girl"], [FOLDER],
                          characters=["rem"])

    images = [i for i in shared.load_gallery()["images"] if i["filename"] == fn]
    assert len(images) == 1
    assert set(images[0]["tags"]["tag"]) == {"blue_hair", "1girl"}
    assert images[0]["tags"]["character"] == ["rem"]


def test_a_download_of_an_already_tagged_file_changes_nothing(lib):
    fn = "abcdef.jpg"
    shared.load_gallery()["images"].append({
        "id": "x", "filename": fn, "filepath": f"gelbooru/{FOLDER}/{fn}",
        "site": "gelbooru",
        "tags": {"artist": [FOLDER], "character": ["rem"], "tag": ["1girl", "smile"]},
        "favourite": True, "downloaded_at": "2026-09-01T00:00:00"})
    shared._gallery_cache["filenames"].add(fn)

    shared.add_to_gallery("gelbooru", fn, f"gelbooru/{FOLDER}/{fn}",
                          ["blue_hair"], [FOLDER])

    entry = next(i for i in shared.load_gallery()["images"] if i["filename"] == fn)
    assert entry["tags"]["tag"] == ["1girl", "smile"]
    assert entry["favourite"] is True


def test_an_exit_persists_the_batched_writes(tmp_path):
    """Nothing saved explicitly: a process that dies must not have lost it.

    Both stores commit as they are written, so there is no batch left for
    atexit to flush — and if either write ever goes lazy again, the next scan
    rebuilds the record folder-name-only and the tags are gone."""
    import json
    import sqlite3
    import subprocess
    import sys

    gallery_db = tmp_path / "gallery.db"
    script = f"""
import core.shared as s
from core.database import DatabaseManager
from core.gallery_store import use_store_path
use_store_path({str(gallery_db)!r})
s.load_gallery()
s.add_to_gallery("gelbooru", "a.jpg", "gelbooru/x/a.jpg", ["blue_hair"], ["artist_x"])
DatabaseManager.add_image_history("gelbooru", "a.jpg", ["blue_hair"], ["artist_x"])
# no save_gallery / flush_image_history call: the exit has to find it there
"""
    subprocess.run([sys.executable, "-c", script], check=True,
                   cwd=shared.BASE_DIR, timeout=60)

    con = sqlite3.connect(gallery_db)
    tags = [r[0] for r in con.execute(
        "SELECT tag FROM gallery_tags WHERE bucket='tag'")]
    hist = [r[0] for r in con.execute(
        "SELECT tags FROM image_history ORDER BY seq DESC LIMIT 1")]
    con.close()
    assert "blue_hair" in tags, "gallery record lost on exit"
    assert hist, "history record lost on exit"
    assert json.loads(hist[0])["tag"] == ["blue_hair"]


def test_sigterm_flushes_before_dying():
    # SIGTERM skips atexit — the app installs its own flush-then-die handler
    import signal

    assert signal.getsignal(signal.SIGTERM) is Rems_Dl._flush_before_sigterm
