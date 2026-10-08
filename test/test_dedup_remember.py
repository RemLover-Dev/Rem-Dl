import asyncio
import os

from core.shared import BaseDownloader, save_history


class _Dup:
    is_duplicate = True


class MockContent:
    def __init__(self, chunks):
        self.chunks = chunks

    async def iter_chunked(self, n):
        for c in self.chunks:
            yield c


class MockResponse:
    def __init__(self, status, headers, content):
        self.status = status
        self.headers = headers
        self.content = content

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError("http error")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass


class MockSession:
    def __init__(self, responses):
        self.headers = {}
        self.responses = responses

    def get(self, url, headers=None, timeout=None):
        return self.responses.pop(0)


def _mk(tmp_path, monkeypatch):
    monkeypatch.setattr("core.shared.MASTER_FOLDER", str(tmp_path))
    monkeypatch.setattr("core.shared.write_image_metadata", lambda *a, **k: None)
    monkeypatch.setattr("core.shared.add_to_gallery", lambda *a, **k: None)
    monkeypatch.setattr("core.shared.send_tags", lambda *a, **k: None)
    return BaseDownloader(
        "test_worker", "Test:Site", 10, {"anti_ban_pause": 0, "download_retries": 1}
    )


def test_dup_killed_name_survives_for_the_next_run(tmp_path, monkeypatch):
    dl = _mk(tmp_path, monkeypatch)
    dl.log = lambda m: None
    monkeypatch.setattr("core.shared.check_duplicate", lambda *a, **k: _Dup())

    payload = b"img-bytes" * 300
    dl.session = MockSession(
        [MockResponse(200, {"Content-Length": str(len(payload))}, MockContent([payload]))]
    )

    filename = "dup-shot.jpg"
    folder = os.path.join(dl.site_root, "Tag")
    os.makedirs(folder, exist_ok=True)
    filepath = os.path.join(folder, filename)

    async def _run():
        return await dl._async_download_file(
            url="http://example.com/x.jpg",
            filepath=filepath,
            filename=filename,
            tags_list=[],
            artists=[],
        )

    ok = asyncio.run(_run())
    assert ok is False
    assert dl.duplicate_count == 1
    assert not os.path.exists(filepath)
    # batched write — the run-end flush (run_async_loop finally) persists it
    assert filename in dl.dl_history
    assert dl._history_dirty >= 1

    # simulate end of run, then a FRESH worker (next session) must skip it
    if dl._history_dirty:
        save_history(dl.site_root, dl.dl_history)

    dl2 = _mk(tmp_path, monkeypatch)
    assert filename in dl2.dl_history
    dl2.download_queue = asyncio.Queue()
    enqueued = asyncio.run(
        dl2.enqueue_download(
            url="http://example.com/x.jpg",
            filepath=filepath,
            filename=filename,
            tags_list=[],
        )
    )
    assert enqueued is False
