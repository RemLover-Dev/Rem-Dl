import asyncio
import hashlib
import os

import aiohttp

from core.shared import BaseDownloader


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


def test_truncated_download_resumes_with_range(monkeypatch, tmp_path):
    monkeypatch.setattr("core.shared.MASTER_FOLDER", str(tmp_path))
    monkeypatch.setattr("core.shared.check_duplicate", lambda *a, **k: None)
    monkeypatch.setattr("core.shared.write_image_metadata", lambda *a, **k: None)
    monkeypatch.setattr("core.shared.add_to_gallery", lambda *a, **k: None)
    monkeypatch.setattr("core.shared.send_tags", lambda *a, **k: None)

    full = bytes(range(256)) * 500
    half = len(full) // 2
    digest = hashlib.md5(full).hexdigest()
    filename = f"payload-{digest}.jpg"

    dl = BaseDownloader(
        "test_worker",
        "Test:Site",
        10,
        {"anti_ban_pause": 0, "download_retries": 3},
    )
    logs = []
    dl.log = logs.append
    dl.session = MockSession(
        [
            MockResponse(
                200,
                {"Content-Length": str(len(full))},
                MockContent([full[:half]], fail_after=True),
            ),
            MockResponse(
                206,
                {"Content-Length": str(len(full) - half)},
                MockContent([full[half:]]),
            ),
        ]
    )

    async def _run():
        folder = os.path.join(dl.site_root, "Safe_Tag")
        os.makedirs(folder, exist_ok=True)
        filepath = os.path.join(folder, filename)
        return await dl._async_download_file(
            url="http://example.com/img.jpg",
            filepath=filepath,
            filename=filename,
            tags_list=["rem"],
            artists=["artist"],
            file_size=len(full),
        )

    ok = asyncio.run(_run())

    assert ok is True
    assert len(dl.session.calls) == 2
    assert "Range" not in dl.session.calls[0]
    assert dl.session.calls[1].get("Range") == f"bytes={half}-"
    filepath = os.path.join(dl.site_root, "Safe_Tag", filename)
    with open(filepath, "rb") as f:
        assert f.read() == full
    assert not os.path.exists(filepath + ".part")
    assert not any("[FAILED]" in m for m in logs)
