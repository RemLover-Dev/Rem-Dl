import asyncio
import io
import os
import time

import pytest
from PIL import Image
from test_download_resume import MockContent, MockResponse, MockSession

from core.shared import BaseDownloader, write_image_metadata


def _pil_can_write_jxl():
    try:
        buf = io.BytesIO()
        Image.new("RGB", (2, 2)).save(buf, format="JXL")
        return True
    except Exception:
        return False


def _await_order(order, n=4, timeout=3.0):
    deadline = time.monotonic() + timeout
    while len(order) < n and time.monotonic() < deadline:
        time.sleep(0.02)


def _png(tmp_path, name="img.png"):
    p = str(tmp_path / name)
    Image.new("RGB", (4, 4), (10, 20, 30)).save(p)
    return p


def test_png_metadata_roundtrip_and_replace(tmp_path):
    p = _png(tmp_path)
    write_image_metadata(p, ["solo"], ["artist_x"], "zerochan")
    text = Image.open(p).info["Rems_Dl"]
    assert "site:zerochan" in text
    assert "artist:artist_x" in text
    assert "tag:solo" in text

    # a second write replaces the old chunk instead of stacking duplicates
    write_image_metadata(p, ["duo"], ["artist_y"], "danbooru")
    with open(p, "rb") as f:
        assert f.read().count(b"Rems_Dl\x00") == 1
    text = Image.open(p).info["Rems_Dl"]
    assert "site:danbooru" in text
    assert "tag:duo" in text
    assert "tag:solo" not in text


def test_png_nonlatin1_tags_roundtrip(tmp_path):
    p = _png(tmp_path)
    write_image_metadata(p, ["\u65e5\u672c\u8a9e\u30bf\u30b0"], [], "zerochan")
    # non-latin1 must go through the iTXt fallback and still read back
    assert "tag:\u65e5\u672c\u8a9e\u30bf\u30b0" in Image.open(p).info["Rems_Dl"]


def test_jpg_metadata_roundtrip(tmp_path):
    p = str(tmp_path / "img.jpg")
    Image.new("RGB", (8, 8)).save(p, quality=90)
    write_image_metadata(p, ["solo"], ["artist_x"], "zerochan")
    with open(p, "rb") as f:
        data = f.read()
    assert data[:2] == b"\xff\xd8"
    i = data.find(b"\xff\xfe")
    assert i != -1
    seg_len = int.from_bytes(data[i + 2:i + 4], "big")
    payload = data[i + 4:i + 2 + seg_len]
    assert payload.startswith(b"Rems_Dl\n")
    assert b"site:zerochan" in payload
    assert Image.open(p).size == (8, 8)  # still a valid image


def test_history_row_lands_before_metadata_and_gallery(monkeypatch, tmp_path):
    """The viewer's tag box reads imageHistory — send_tags must run before the
    metadata write it used to wait behind."""
    monkeypatch.setattr("core.shared.MASTER_FOLDER", str(tmp_path))
    monkeypatch.setattr("core.shared.check_duplicate", lambda *a, **k: None)
    monkeypatch.setattr("core.shared.PUBLISH_GRACE_S", 0.5)
    order = []
    monkeypatch.setattr("core.shared.send_tags", lambda *a, **k: order.append("send_tags"))
    monkeypatch.setattr("core.shared.write_image_metadata", lambda *a, **k: order.append("write"))
    monkeypatch.setattr("core.shared.add_to_gallery", lambda *a, **k: order.append("gallery"))

    payload = b"jpgbytes" * 100
    dl = BaseDownloader("test_worker", "Test:Site", 1,
                        {"anti_ban_pause": 0, "download_retries": 1})
    dl.log = lambda m: order.append("log:SUCCESS") if "[SUCCESS]" in m else None
    dl.session = MockSession([
        MockResponse(200, {"Content-Length": str(len(payload))},
                     MockContent([payload])),
    ])

    async def _run():
        folder = os.path.join(dl.site_root, "Safe_Tag")
        os.makedirs(folder, exist_ok=True)
        return await dl._async_download_file(
            url="http://example.com/img.jpg",
            filepath=os.path.join(folder, "x.jpg"),
            filename="x.jpg",
            tags_list=["solo"],
            artists=["a"],
            file_size=len(payload),
        )

    assert asyncio.run(_run()) is True
    # metadata is baked into the bytes immediately — before any auto-jxl
    # conversion can consume the file — while the UI publication comes later
    assert order == ["write"]
    _await_order(order)
    assert order == ["write", "send_tags", "gallery", "log:SUCCESS"]


def test_publish_discarded_when_file_replaced_in_grace_window(monkeypatch, tmp_path):
    """A keep-larger swap can delete a newly downloaded file within the publish
    grace window — its cards must never appear in any view."""
    monkeypatch.setattr("core.shared.MASTER_FOLDER", str(tmp_path))
    monkeypatch.setattr("core.shared.check_duplicate", lambda *a, **k: None)
    monkeypatch.setattr("core.shared.PUBLISH_GRACE_S", 0.15)
    order = []
    monkeypatch.setattr("core.shared.send_tags", lambda *a, **k: order.append("send_tags"))
    monkeypatch.setattr("core.shared.write_image_metadata", lambda *a, **k: order.append("write"))
    monkeypatch.setattr("core.shared.add_to_gallery", lambda *a, **k: order.append("gallery"))

    payload = b"jpgbytes" * 100
    dl = BaseDownloader("test_worker", "Test:Site", 1,
                        {"anti_ban_pause": 0, "download_retries": 1})
    dl.log = lambda m: order.append("log:SUCCESS") if "[SUCCESS]" in m else None
    dl.session = MockSession([
        MockResponse(200, {"Content-Length": str(len(payload))},
                     MockContent([payload])),
    ])

    async def _run():
        folder = os.path.join(dl.site_root, "Safe_Tag")
        os.makedirs(folder, exist_ok=True)
        fp = os.path.join(folder, "x.jpg")
        res = await dl._async_download_file(
            url="http://example.com/img.jpg",
            filepath=fp,
            filename="x.jpg",
            tags_list=["solo"],
            artists=["a"],
            file_size=len(payload),
        )
        # the swap runs before the timer fires
        os.remove(fp)
        return res

    assert asyncio.run(_run()) is True
    assert order == ["write"]
    time.sleep(0.35)
    assert order == ["write"]


def test_held_publish_waits_for_its_own_conversion(monkeypatch, tmp_path):
    """auto-jxl: cards publish only when THIS file's conversion releases them;
    another file's release must not pull this one into view. Media is baked
    at download time and skipped at publish (the .jxl carries it)."""
    import io

    import core.shared as sh
    if not _pil_can_write_jxl():
        pytest.skip("PIL without the JXL plugin — fake .jxl files can't be written")

    monkeypatch.setattr("core.shared.MASTER_FOLDER", str(tmp_path))
    monkeypatch.setattr("core.shared.check_duplicate", lambda *a, **k: None)
    monkeypatch.setattr("core.shared.PUBLISH_GRACE_S", 0.05)
    order = []
    monkeypatch.setattr("core.shared.send_tags", lambda *a, **k: order.append("send_tags:" + (a[1] or "")))
    monkeypatch.setattr("core.shared.write_image_metadata", lambda *a, **k: order.append("write"))

    payload = b"jpgbytes" * 100
    dl = BaseDownloader("test_worker", "Test:Site", 1,
                        {"anti_ban_pause": 0, "download_retries": 1})
    dl.log = lambda m: order.append("log:" + (os.path.basename(m.split("|PATH| ")[1].split(" |TAGS|")[0]) if "|PATH|" in m else m)) if "[SUCCESS]" in m else None
    dl.session = MockSession([
        MockResponse(200, {"Content-Length": str(len(payload))},
                     MockContent([payload])),
    ])

    async def _download(fp, fname):
        folder = os.path.join(dl.site_root, "Safe_Tag")
        os.makedirs(folder, exist_ok=True)
        return await dl._async_download_file(
            url="http://example.com/img.jpg", filepath=fp, filename=fname,
            tags_list=["solo"], artists=["a"], file_size=len(payload))

    # first download holds forever (held flag set externally, as the hook does)
    fp_a = str(tmp_path / "a.jpg")
    Image.new("RGB", (4, 4)).save(fp_a)
    payload_write = b"jpgbytes" * 100
    dl.session = MockSession([MockResponse(200, {"Content-Length": str(len(payload_write))}, MockContent([payload_write]))])
    assert asyncio.run(_download(fp_a, "a.jpg")) is True
    sh.mark_download_held(os.path.abspath(fp_a))

    # second download: its conversion finishes first — releasing B's spec only
    fp_b = str(tmp_path / "b.jpg")
    Image.new("RGB", (4, 4)).save(fp_b)
    payload_write = b"jpgbytes" * 100
    dl.session = MockSession([MockResponse(200, {"Content-Length": str(len(payload_write))}, MockContent([payload_write]))])
    assert asyncio.run(_download(fp_b, "b.jpg")) is True
    sh.mark_download_held(os.path.abspath(fp_b))
    jxl_b = str(tmp_path / "b.jxl")
    Image.new("RGB", (4, 4)).save(jxl_b)
    os.remove(fp_b)
    assert sh.release_download_publish(os.path.abspath(fp_b), jxl_b) is True

    time.sleep(0.3)
    # only B published, against the .jxl, without a second metadata write;
    # A stayed silent
    assert order == ["write", "write", "send_tags:b.jxl", "log:b.jxl"]
