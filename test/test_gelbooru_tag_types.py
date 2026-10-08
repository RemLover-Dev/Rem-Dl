import asyncio

from workers.gelbooru import GelbooruWorker


class _Resp:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def json(self):
        return self._payload


class _Session:
    """Fake gelbooru tag index: exact-name echo with a type, plus failure injection."""

    def __init__(self, fail_first=None):
        self.calls = []
        self.fail_first = dict(fail_first or {})

    async def get(self, url, params=None):
        name = (params or {}).get("name", "")
        self.calls.append(name)
        if self.fail_first.get(name, 0) > 0:
            self.fail_first[name] -= 1
            return _Resp(500, {})
        return _Resp(200, {"tag": [{"name": name, "type": 1}]})


def _worker(tmp_path, monkeypatch):
    monkeypatch.setattr("core.shared.MASTER_FOLDER", str(tmp_path))
    monkeypatch.setattr("core.shared.TAG_CACHE_DIR", str(tmp_path))

    async def _fast(_delay):
        return None

    monkeypatch.setattr("workers.gelbooru.asyncio.sleep", _fast)
    return GelbooruWorker("ines (arknights)", 0, "", "", {})


def test_tag_fetch_covers_every_uncached_tag(tmp_path, monkeypatch):
    """The old [:150] cap left the rest of a page's tags uncategorized, and
    that wrong default was frozen into downloaded files' metadata."""
    w = _worker(tmp_path, monkeypatch)
    tags = [f"tag_{i:04d}" for i in range(300)]
    w.session = _Session()

    asyncio.run(w._fetch_tag_types(tags))

    assert len(w.session.calls) == 300
    assert all(w.tag_cache[t] == "artist" for t in tags)


def test_tag_fetch_retries_transient_failures(tmp_path, monkeypatch):
    w = _worker(tmp_path, monkeypatch)
    w.session = _Session(fail_first={"uof": 2})  # two 500s, third attempt ok

    asyncio.run(w._fetch_tag_types(["uof"]))

    assert w.tag_cache["uof"] == "artist"
    assert w.session.calls.count("uof") == 3


def test_tag_fetch_permanent_failure_stays_uncached(tmp_path, monkeypatch):
    """A tag that never comes back must not be cached as 'tag' — that would
    mislabel it forever; it retries next run instead."""
    w = _worker(tmp_path, monkeypatch)
    w.session = _Session(fail_first={"uof": 99})

    asyncio.run(w._fetch_tag_types(["uof"]))

    assert "uof" not in w.tag_cache
    assert w.session.calls.count("uof") == 3
