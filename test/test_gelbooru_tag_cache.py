import asyncio
import json

import workers.gelbooru as gelbooru_mod
from workers.gelbooru import GelbooruWorker

NET_CONFIG = {
    "anti_ban_pause": 0.0,
    "download_retries": 1,
    "use_proxy": False,
    "api_timeout": 5,
}


def _worker(tmp_path, monkeypatch):
    monkeypatch.setattr("core.shared.MASTER_FOLDER", str(tmp_path))
    monkeypatch.setattr(gelbooru_mod, "TAG_TYPES_FILE", str(tmp_path / "gelbooru_tag_types.json"))
    return GelbooruWorker("rem", 10, "", [], NET_CONFIG)


def test_cache_loaded_from_disk(tmp_path, monkeypatch):
    (tmp_path / "gelbooru_tag_types.json").write_text(json.dumps({"rem": "artist"}))
    w = _worker(tmp_path, monkeypatch)
    assert w.tag_cache["rem"] == "artist"  # known tags must not refetch


def test_fetch_persists_verified_types(tmp_path, monkeypatch):
    w = _worker(tmp_path, monkeypatch)
    assert w.tag_cache == {}

    class _Resp:
        status = 200

        async def json(self):
            return {"tag": [{"name": "mika_pikazo", "type": 1}]}

    class _Session:
        async def get(self, *a, **k):
            return _Resp()

    w.session = _Session()
    asyncio.run(w._fetch_tag_types(["mika_pikazo"]))
    assert w.tag_cache["mika_pikazo"] == "artist"

    saved = json.loads((tmp_path / "gelbooru_tag_types.json").read_text())
    assert saved == {"mika_pikazo": "artist"}

    # a later run starts warm — this is the whole point of the file
    w2 = _worker(tmp_path, monkeypatch)
    assert w2.tag_cache == {"mika_pikazo": "artist"}
    asyncio.run(w2._fetch_tag_types(["mika_pikazo", "brand_new_tag"]))
    # known tag fetched nothing; unknown/no-match stays uncached (retried next run)
    assert w2.tag_cache["mika_pikazo"] == "artist"
    assert "brand_new_tag" not in w2.tag_cache


async def _no_sleep(*_a, **_k):
    return None


def test_failed_fetch_is_retried(tmp_path, monkeypatch):
    w = _worker(tmp_path, monkeypatch)

    class _Resp:
        status = 200

        async def json(self):
            return {"tag": [{"name": "mika_pikazo", "type": 1}]}

    class _Session:
        def __init__(self):
            self.calls = 0

        async def get(self, *a, **k):
            self.calls += 1
            if self.calls < 3:
                raise RuntimeError("flaky network")
            return _Resp()

    w.session = _Session()
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    asyncio.run(w._fetch_tag_types(["mika_pikazo"]))
    assert w.session.calls == 3  # two failures, third attempt landed
    assert w.tag_cache["mika_pikazo"] == "artist"  # verified → cached


def test_gives_up_after_three_attempts(tmp_path, monkeypatch):
    w = _worker(tmp_path, monkeypatch)

    class _Resp:
        status = 429

        async def json(self):
            return {}

    class _Session:
        def __init__(self):
            self.calls = 0

        async def get(self, *a, **k):
            self.calls += 1
            return _Resp()

    w.session = _Session()
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    asyncio.run(w._fetch_tag_types(["mika_pikazo"]))
    assert w.session.calls == 3
    assert w.tag_cache == {}  # failure never cached — next run retries
