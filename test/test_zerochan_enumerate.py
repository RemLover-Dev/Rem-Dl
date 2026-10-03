import asyncio

from workers.zerochan import ZerochanWorker, _looks_like_cf_challenge

CF_HTML = (
    '<html><head><meta http-equiv="refresh" content="3">'
    "<title>Checking browser...</title></head>"
    "<body><h1>Checking browser...</h1><p>Make sure cookies are enabled "
    "and JavaScript is not blocked.</p></body></html>"
)


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self.content = body if isinstance(body, bytes) else body.encode()


class _Session:
    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    def get(self, url, **kw):
        self.calls.append((url, dict(kw.get("headers") or {})))
        return self.resp


def _worker(tmp_path, monkeypatch):
    monkeypatch.setattr("core.shared.MASTER_FOLDER", str(tmp_path))
    w = ZerochanWorker("Ines (Arknights)", 0, {"anti_ban_pause": 0})
    logs = []
    w.log = logs.append
    w.download_queue = asyncio.Queue()
    return w, logs


def test_cf_marker_matches_zerochan_interstitial():
    assert _looks_like_cf_challenge(503, CF_HTML)
    assert _looks_like_cf_challenge(403, CF_HTML)
    assert not _looks_like_cf_challenge(200, CF_HTML)
    assert not _looks_like_cf_challenge(503, "<h1>Internal Server Error</h1>")


def test_json_enumerate_engine_status_matrix(tmp_path, monkeypatch):
    w, _ = _worker(tmp_path, monkeypatch)
    sess = _Session(_Resp(200, '{"items": []}'))
    monkeypatch.setattr(w, "_ensure_curl_session", lambda: sess)

    # Empty page with HTTP 200 = real end-of-data, not an engine failure.
    items, ok = asyncio.run(w._curl_json_enumerate(page=3))
    assert (items, ok) == ([], True)

    # gallery-dl UA is what zerochan lets through; it must be on the wire.
    _, headers = sess.calls[0]
    assert headers.get("User-Agent") == "gallery-dl"

    sess.resp = _Resp(200, '{"items": [{"id": 1, "tags": ["Solo"]}]}')
    items, ok = asyncio.run(w._curl_json_enumerate(page=1))
    assert ok is True and len(items) == 1

    # CF interstitial and HTTP errors are engine failures, never EOD.
    sess.resp = _Resp(503, CF_HTML)
    assert asyncio.run(w._curl_json_enumerate(page=2)) == ([], False)

    sess.resp = _Resp(500, "boom")
    assert asyncio.run(w._curl_json_enumerate(page=2)) == ([], False)

    sess.resp = _Resp(200, "<html>not json at all</html>")
    assert asyncio.run(w._curl_json_enumerate(page=2)) == ([], False)


def _wire_scraper(monkeypatch, w, fake_gdl, fake_json, fake_detail=None):
    async def noop():
        return None

    monkeypatch.setattr(
        "workers.zerochan._ensure_gallery_dl_patch", lambda log=None: True
    )
    monkeypatch.setattr(w, "_try_login", noop)
    monkeypatch.setattr(w, "_ensure_curl_session", lambda: None)
    monkeypatch.setattr(w, "_gallery_dl_enumerate", fake_gdl)
    monkeypatch.setattr(w, "_curl_json_enumerate", fake_json)
    if fake_detail is not None:
        monkeypatch.setattr(w, "_curl_post_detail", fake_detail)

    async def fake_probe(pid):
        return f"https://static.zerochan.net/.full.{pid}.jpg"

    monkeypatch.setattr(w, "_probe_static_url", fake_probe)


def _filenames(w):
    return [t[2] for t in w.download_queue._queue]


def test_scraper_survives_gallery_dl_page2_failure(tmp_path, monkeypatch):
    """The 'only 4 of 100' bug: gallery-dl failing/empty after page 1 must
    switch to the JSON API at a realigned page, not stop enumeration."""
    w, logs = _worker(tmp_path, monkeypatch)
    json_calls = []

    def fake_gdl(tag, page, size):
        if page == 1:
            return ([{"id": i, "file_url": f"https://static.zerochan.net/.full.{i}.jpg",
                      "tags": ["Solo"]} for i in range(1, 8)], True)
        return ([], True)  # rc!=0 with no fatal marker: engine "ok", no posts

    async def fake_json(page=1, per_page=200):
        json_calls.append((page, per_page))
        if page == 1:
            return ([{"id": i, "tags": ["Solo"]} for i in range(1, 31)], True)
        return ([], True)

    async def fake_detail(pid):
        return {"full": f"https://static.zerochan.net/.full.{pid}.jpg"}

    _wire_scraper(monkeypatch, w, fake_gdl, fake_json, fake_detail)
    asyncio.run(w.scraper_task())

    # gallery-dl served 1..7, JSON resumed at page 1 (ids 1..30 overlap 1..7,
    # filename dedupe absorbs them) instead of quitting after 7.
    assert json_calls[0] == (1, 200)
    assert len(_filenames(w)) == 30
    joined = "\n".join(logs)
    assert "No more posts available from gallery-dl" not in joined
    assert "No more posts available." in joined


def test_scraper_reports_api_failure_not_end_of_data(tmp_path, monkeypatch):
    w, logs = _worker(tmp_path, monkeypatch)

    def fake_gdl(tag, page, size):
        return ([], False)

    async def fake_json(page=1, per_page=200):
        return [], False  # CF challenge on every attempt

    _wire_scraper(monkeypatch, w, fake_gdl, fake_json)
    asyncio.run(w.scraper_task())

    joined = "\n".join(logs)
    assert "JSON API failed" in joined
    assert "NOT the end" in joined
    assert "No posts found for" not in joined
    assert "No more posts available." not in joined
    assert len(_filenames(w)) == 0


def test_scraper_crosschecks_when_gallery_dl_returns_empty(tmp_path, monkeypatch):
    w, logs = _worker(tmp_path, monkeypatch)

    def fake_gdl(tag, page, size):
        return ([], True)  # ran clean, zero posts (tag end OR its l=200 cap)

    async def fake_json(page=1, per_page=200):
        if page == 1:
            return ([{"id": i, "tags": ["Solo"]} for i in range(6, 11)], True)
        return ([], True)

    async def fake_detail(pid):
        return {"full": f"https://static.zerochan.net/.full.{pid}.jpg"}

    _wire_scraper(monkeypatch, w, fake_gdl, fake_json, fake_detail)
    asyncio.run(w.scraper_task())

    assert len(_filenames(w)) == 5
    joined = "\n".join(logs)
    assert "cross-checking" in joined
    assert "No posts found for" not in joined
    assert "No more posts available." in joined


def test_scraper_empty_tag_still_reports_bad_spelling(tmp_path, monkeypatch):
    w, logs = _worker(tmp_path, monkeypatch)

    def fake_gdl(tag, page, size):
        return ([], True)

    async def fake_json(page=1, per_page=200):
        return ([], True)

    _wire_scraper(monkeypatch, w, fake_gdl, fake_json)
    asyncio.run(w.scraper_task())

    joined = "\n".join(logs)
    assert "No posts found for" in joined
    assert "Check the tag spelling" in joined
