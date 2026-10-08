import asyncio
import json

import workers.gsbooru as gsbooru_mod
import workers.konachan as konachan_mod
import workers.safebooru as safebooru_mod
import workers.yande as yande_mod
from workers.gsbooru import GsbooruWorker
from workers.konachan import KonachanWorker
from workers.safebooru import SafebooruWorker
from workers.yande import YandeWorker

NET_CONFIG = {
    "anti_ban_pause": 0.0,
    "download_retries": 1,
    "use_proxy": False,
    "api_timeout": 5,
}

XML = '<?xml version="1.0"?><tags><tag id="1" name="mika_pikazo" type="1" count="5"/></tags>'


class _TextResp:
    status = 200

    async def text(self):
        return XML


class _Session:
    async def get(self, *a, **k):
        return _TextResp()


def _patch(tmp_path, monkeypatch, mod, filename):
    monkeypatch.setattr("core.shared.MASTER_FOLDER", str(tmp_path))
    monkeypatch.setattr(mod, "TAG_TYPES_FILE", str(tmp_path / filename))


def test_konachan_persists(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch, konachan_mod, "konachan_tag_types.json")
    w = KonachanWorker("rem", 10, "", [], NET_CONFIG)

    class _Resp:
        status = 200

        async def json(self):
            return [{"name": "mika_pikazo", "type": 1}]

    class _Session:
        async def get(self, *a, **k):
            return _Resp()

    w.session = _Session()
    asyncio.run(w._fetch_tag_types(["mika_pikazo"]))
    assert w.tag_cache["mika_pikazo"] == "artist"
    saved = json.loads((tmp_path / "konachan_tag_types.json").read_text())
    assert saved == {"mika_pikazo": "artist"}
    # warm start on a later run
    w2 = KonachanWorker("rem", 10, "", [], NET_CONFIG)
    assert w2.tag_cache == {"mika_pikazo": "artist"}


def test_yande_persists(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch, yande_mod, "yande_tag_types.json")
    w = YandeWorker("rem", 10, "", NET_CONFIG)
    w.session = _Session()
    asyncio.run(w._fetch_tag_types(["mika_pikazo"]))
    assert w.tag_cache["mika_pikazo"] == 1  # yande stores raw int type codes
    saved = json.loads((tmp_path / "yande_tag_types.json").read_text())
    assert saved == {"mika_pikazo": 1}
    w2 = YandeWorker("rem", 10, "", NET_CONFIG)
    assert w2.tag_cache == {"mika_pikazo": 1}


def test_safebooru_persists(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch, safebooru_mod, "safebooru_tag_types.json")
    w = SafebooruWorker("rem", 10, [], NET_CONFIG)
    w.session = _Session()
    asyncio.run(w._fetch_tag_types(["mika_pikazo"]))
    assert w.tag_cache["mika_pikazo"] == 1
    saved = json.loads((tmp_path / "safebooru_tag_types.json").read_text())
    assert saved == {"mika_pikazo": 1}
    w2 = SafebooruWorker("rem", 10, [], NET_CONFIG)
    assert w2.tag_cache == {"mika_pikazo": 1}


def test_gsbooru_persists(tmp_path, monkeypatch):
    _patch(tmp_path, monkeypatch, gsbooru_mod, "gsbooru_tag_types.json")
    monkeypatch.setenv("GSBOORU_API_KEY", "test-key")
    w = GsbooruWorker("rem", 10, "", [], NET_CONFIG)

    class _Resp:
        status = 200

        async def json(self, content_type=None):
            return {"status": "ok",
                    "tags": [{"name": "mika_pikazo", "type": 1}]}

    class _CM:
        async def __aenter__(self):
            return _Resp()

        async def __aexit__(self, *a):
            return False

    class _Session:
        def get(self, *a, **k):
            return _CM()

    w.session = _Session()
    asyncio.run(w._fetch_tag_types(["mika_pikazo"]))
    assert w.tag_cache["mika_pikazo"] == "artist"
    saved = json.loads((tmp_path / "gsbooru_tag_types.json").read_text())
    assert saved == {"mika_pikazo": "artist"}
    w2 = GsbooruWorker("rem", 10, "", [], NET_CONFIG)
    assert w2.tag_cache == {"mika_pikazo": "artist"}
