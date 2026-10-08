"""nekos.best image results carry artist_name — it must ride the queue as the artist bucket."""
import asyncio

from workers.nekos_best import NekosBestWorker

NET_CONFIG = {"anti_ban_pause": 0.0, "download_retries": 1, "use_proxy": False, "api_timeout": 5}


class _Resp:
    def __init__(self, payload):
        self._p = payload
        self.status = 200

    def raise_for_status(self):
        pass

    async def json(self):
        return self._p


class _Session:
    def __init__(self, payload):
        self._p = payload

    async def get(self, url):
        return _Resp(self._p)


def _worker(tmp_path, monkeypatch, payload, category="neko", amount=1):
    monkeypatch.setattr("core.shared.MASTER_FOLDER", str(tmp_path))
    w = NekosBestWorker(category, amount, NET_CONFIG)
    w.session = _Session(payload)
    calls = []

    async def fake_enqueue(url, filepath, filename, tags_list, artists=None, copyrights=None, **kw):
        calls.append((tags_list, artists, copyrights or []))
        return True

    w.enqueue_download = fake_enqueue
    return w, calls


def test_artist_name_becomes_artist_bucket(tmp_path, monkeypatch):
    w, calls = _worker(tmp_path, monkeypatch, {"results": [
        {"url": "https://nekos.best/api/v2/neko/abc.png",
         "artist_name": "John Doe", "artist_href": "https://x/1", "source_url": "https://x/2",
         "dimensions": {"width": 1, "height": 1}}]})
    asyncio.run(w.scraper_task())
    assert calls == [(["neko"], ["John Doe"], [])], calls


def test_gif_anime_name_becomes_copyright_tag(tmp_path, monkeypatch):
    w, calls = _worker(tmp_path, monkeypatch, {"results": [
        {"url": "https://nekos.best/api/v2/hug/xyz.gif",
         "anime_name": "Generic Anime", "dimensions": {"width": 1, "height": 1}}]}, category="hug")
    asyncio.run(w.scraper_task())
    assert calls == [(["hug"], [], ["Generic Anime"])], calls


def test_blank_artist_treated_as_missing(tmp_path, monkeypatch):
    # category tag comes from the worker's configured category, not the URL
    w, calls = _worker(tmp_path, monkeypatch, {"results": [
        {"url": "https://nekos.best/api/v2/neko/abc.png", "artist_name": ""}]})
    asyncio.run(w.scraper_task())
    assert calls == [(["neko"], [], [])], calls
