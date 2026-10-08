import asyncio
import os

import aiohttp
from PIL import Image

from core.shared import BaseDownloader, write_image_metadata


class MockContent:
    def __init__(self, chunks, fail_after=False):
        self.chunks = chunks
        self.fail_after = fail_after

    async def iter_chunked(self, n):
        for c in self.chunks:
            yield c
        if self.fail_after:
            raise aiohttp.ClientPayloadError("Response payload is not completed")


class MockResponse:
    def __init__(self, status, headers, content):
        self.status = status
        self.headers = headers
        self.content = content

    def raise_for_status(self):
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                request_info=None, history=(), status=self.status
            )

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


class MockSession:
    def __init__(self, responses):
        self.headers = {}
        self.responses = responses
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(dict(headers or {}))
        return self.responses.pop(0)


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
    assert order == ["send_tags", "write", "gallery", "log:SUCCESS"]
