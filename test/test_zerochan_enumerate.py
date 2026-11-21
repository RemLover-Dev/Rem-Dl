import asyncio

from workers.zerochan import ZerochanWorker, _derive_img_url, _looks_like_cf_challenge

CF_HTML = (
    '<html><head><meta http-equiv="refresh" content="3">'
    "<title>Checking browser...</title></head>"
    "<body><h1>Checking browser...</h1><p>Make sure cookies are enabled "
    "and JavaScript is not blocked.</p></body></html>"
)

POST_HTML = """<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"ImageObject",
 "contentUrl":"https://static.zerochan.net/Remilia.Scarlet.full.3943142.png",
 "width":"1947","height":"3718"}
</script>
<script type="application/ld+json">{"@type":"BreadcrumbList"}</script>
</head><body><ul id="tags" class="tags">
<li class="mangaka" data-tag="Torinari"><a href="/Torinari">Torinari</a></li>
<li class="series" data-tag="Touhou"><a href="/Touhou">Touhou</a></li>
<li class="character primary" data-tag="Remilia Scarlet"><a href="/Remilia+Scarlet">Remilia Scarlet</a></li>
<li class="theme" data-tag="Female"><a href="/Female">Female</a></li>
</ul></body></html>"""


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self.content = body if isinstance(body, bytes) else body.encode()
        self.text = body if isinstance(body, str) else body.decode()

    @property
    def headers(self):
        return {}


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


def _wire_scraper(monkeypatch, w, fake_json, fake_xml=None, fake_page=None,
                  fake_gdl=None):
    async def noop():
        return None

    def unexpected_gdl(*a, **k):
        raise AssertionError("gallery-dl must not run without credentials")

    def gdl_ok(*a, **k):
        return [], False

    async def no_xml(page=1):
        return [], False

    async def no_page(pid):
        return "", ""

    monkeypatch.setattr(w, "_try_login", noop)
    monkeypatch.setattr(w, "_ensure_curl_session", lambda: None)
    monkeypatch.setattr(w, "_gallery_dl_enumerate", fake_gdl or unexpected_gdl)
    monkeypatch.setattr(w, "_curl_json_enumerate", fake_json)
    monkeypatch.setattr(w, "_curl_xml_enumerate", fake_xml or no_xml)
    monkeypatch.setattr(w, "_fetch_post_page", fake_page or no_page)

    async def fake_probe(pid):
        return f"https://static.zerochan.net/.full.{pid}.jpg"

    monkeypatch.setattr(w, "_probe_static_url", fake_probe)


def _filenames(w):
    return [t[2] for t in w.download_queue._queue]


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

    # Zerochan's API rules ask clients to identify themselves.
    _, headers = sess.calls[0]
    assert headers.get("User-Agent").startswith("Rems-Dl - ")

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


def test_json_enumerate_recovers_wrapped_payload(tmp_path, monkeypatch):
    # Zerochan's broken template: stray "{" + CRLF, real object, HTML tail,
    # then a closing "}" — served with HTTP 500. The payload is still good.
    wrapped = ('{\r\n{"items": [{"id": 3943142, "md5": "abc"}]}\r\n'
               '</ul>\r\n}\r\n')
    w, _ = _worker(tmp_path, monkeypatch)
    monkeypatch.setattr(w, "_ensure_curl_session", lambda: _Session(_Resp(500, wrapped)))
    items, ok = asyncio.run(w._curl_json_enumerate(page=1))
    assert ok is True and items == [{"id": 3943142, "md5": "abc"}]

    # garbage without an items payload stays an engine failure
    sess = _Session(_Resp(500, "{ broken"))
    monkeypatch.setattr(w, "_ensure_curl_session", lambda: sess)
    assert asyncio.run(w._curl_json_enumerate(page=1)) == ([], False)


XML_FEED = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/" xmlns:atom="http://www.w3.org/2005/Atom">
<channel>
  <atom:link rel="next" href="?xml=1&amp;p=2" />
  <item>
    <title>Remilia Scarlet</title>
    <link>http://www.zerochan.net/3943142</link>
    <guid>http://www.zerochan.net/3943142</guid>
    <media:content url="https://s1.zerochan.net/600/42/12/3943142.jpg" width="1947" height="3718" expression="sample" />
    <media:keywords>
    \tFemale, Fanart, Mob
\tCap, Remilia Scarlet
    </media:keywords>
  </item>
</channel></rss>"""


def test_xml_enumerate_parses_feed(tmp_path, monkeypatch):
    w, _ = _worker(tmp_path, monkeypatch)
    monkeypatch.setattr(w, "_ensure_curl_session", lambda: _Session(_Resp(200, XML_FEED)))
    posts, ok = asyncio.run(w._curl_xml_enumerate(page=1))
    assert ok is True
    assert posts == [{"id": 3943142, "width": 1947, "height": 3718,
                      "tags": ["Female", "Fanart", "Mob Cap", "Remilia Scarlet"]}]

    # a broken feed is an engine failure, not the end of the tag
    monkeypatch.setattr(w, "_ensure_curl_session", lambda: _Session(_Resp(500, "<rss")))
    assert asyncio.run(w._curl_xml_enumerate(page=1)) == ([], False)


def test_extract_jsonld_picks_the_content_url():
    assert ZerochanWorker._extract_jsonld(POST_HTML)["contentUrl"].endswith(
        "Remilia.Scarlet.full.3943142.png")
    assert ZerochanWorker._extract_jsonld("<html>no ld+json</html>") is None
    assert ZerochanWorker._extract_jsonld('') is None


def test_fetch_post_page_returns_full_url_and_html(tmp_path, monkeypatch):
    w, logs = _worker(tmp_path, monkeypatch)
    monkeypatch.setattr(w, "_ensure_curl_session", lambda: _Session(_Resp(200, POST_HTML)))
    html, url = asyncio.run(w._fetch_post_page(3943142))
    assert html == POST_HTML
    assert url == "https://static.zerochan.net/Remilia.Scarlet.full.3943142.png"

    monkeypatch.setattr(w, "_ensure_curl_session", lambda: _Session(_Resp(500, "boom")))
    assert asyncio.run(w._fetch_post_page(1)) == ("", "")
    assert "page fetch failed (HTTP 500)" in "\n".join(logs)


def test_scraper_uses_listing_plus_one_page_fetch_per_post(tmp_path, monkeypatch):
    w, _logs = _worker(tmp_path, monkeypatch)
    fetched = []

    async def fake_json(page=1, per_page=200):
        if page == 1:
            return ([{"id": 3943142, "md5": "a" * 32, "tags": ["Female"]}], True)
        return [], True

    async def fake_page(pid):
        fetched.append(pid)
        return POST_HTML, "https://static.zerochan.net/Remilia.Scarlet.full.3943142.png"

    _wire_scraper(monkeypatch, w, fake_json, fake_page=fake_page)
    asyncio.run(w.scraper_task())

    assert fetched == [3943142]
    assert len(w.download_queue._queue) == 1
    url, filepath, filename, tags, artists, _size, characters, copyrights = \
        w.download_queue._queue[0][:8]
    assert url.endswith("Remilia.Scarlet.full.3943142.png")
    assert filename == "Remilia.Scarlet.full.3943142.png"
    assert filepath.endswith(filename)
    assert tags == ["Female"]
    assert artists == ["Torinari"]
    assert characters == ["Remilia Scarlet"]
    assert copyrights == ["Touhou"]


def test_scraper_falls_back_to_xml_when_json_dies(tmp_path, monkeypatch):
    w, logs = _worker(tmp_path, monkeypatch)
    calls = []

    async def fake_json(page=1, per_page=200):
        return [], False  # JSON engine down (500 / CF)

    async def fake_xml(page=1):
        calls.append(page)
        if page == 1:
            return ([{"id": i, "tags": ["Solo"]} for i in range(1, 4)], True)
        return [], True

    _wire_scraper(monkeypatch, w, fake_json, fake_xml=fake_xml)
    asyncio.run(w.scraper_task())

    assert calls == [1, 2]
    assert len(_filenames(w)) == 3
    joined = "\n".join(logs)
    assert "falling back to the XML feed" in joined
    assert "JSON API failed" not in joined
    assert "No posts found for" not in joined


def test_posts_forward_md5_to_the_source_hash_gate(tmp_path, monkeypatch):
    """The API's md5 must reach the shared pre-download claim, so a post that
    is already stored under a different filename is dropped before transfer."""
    w, _ = _worker(tmp_path, monkeypatch)
    seen = []

    def fake_claim(filepath, source_md5, *_args, **_kwargs):
        seen.append((filepath, source_md5))
        return False  # already stored / in flight

    monkeypatch.setattr(w, "_claim_source_md5", fake_claim)

    def post(pid, md5):
        return {"id": pid, "md5": md5, "tags": ["Solo"],
                "file_url": f"https://static.zerochan.net/.full.{pid}.jpg"}

    assert asyncio.run(w._enqueue_post_dict(post(3931632, "a" * 32))) is False
    assert _filenames(w) == []
    assert seen[0][1] == "a" * 32

    # a post the gate lets through still enqueues
    monkeypatch.setattr(w, "_claim_source_md5",
                        lambda filepath, source_md5, *a, **k: True)
    assert asyncio.run(w._enqueue_post_dict(post(1111111, "b" * 32))) is True
    assert len(_filenames(w)) == 1


def test_scraper_reports_api_failure_not_end_of_data(tmp_path, monkeypatch):
    w, logs = _worker(tmp_path, monkeypatch)

    async def fake_json(page=1, per_page=200):
        return [], False  # CF challenge on every attempt

    _wire_scraper(monkeypatch, w, fake_json)
    asyncio.run(w.scraper_task())

    joined = "\n".join(logs)
    assert "Zerochan API failed" in joined
    assert "NOT the end" in joined
    assert "No posts found for" not in joined
    assert "No more posts available." not in joined
    assert len(_filenames(w)) == 0


def test_scraper_empty_tag_still_reports_bad_spelling(tmp_path, monkeypatch):
    w, logs = _worker(tmp_path, monkeypatch)

    async def fake_json(page=1, per_page=200):
        return [], True

    _wire_scraper(monkeypatch, w, fake_json)
    asyncio.run(w.scraper_task())

    joined = "\n".join(logs)
    assert "No posts found for" in joined
    assert "Check the tag spelling" in joined


def test_derive_img_url_never_returns_source_page():
    # "source" is a pixiv page URL — returning it downloaded HTML, not an image
    pixiv_page = "https://www.pixiv.net/artworks/123456"
    assert _derive_img_url({"id": 5, "source": pixiv_page}) is None
    assert _derive_img_url({"id": 5}) is None
    full = "https://static.zerochan.net/.full.5.png"
    assert _derive_img_url({"full": full}) == full


def test_page_fetch_failure_falls_back_to_probe_without_dotfiles(tmp_path, monkeypatch):
    """If the post page dies, the static probe still yields a usable filename."""
    w, _logs = _worker(tmp_path, monkeypatch)

    async def fake_json(page=1, per_page=200):
        if page == 1:
            return ([{"id": i, "source": "https://www.pixiv.net/artworks/%d" % i,
                      "tags": ["Solo"]} for i in range(1, 4)], True)
        return [], True

    _wire_scraper(monkeypatch, w, fake_json)
    asyncio.run(w.scraper_task())

    assert _filenames(w) == ["full.1.jpg", "full.2.jpg", "full.3.jpg"]


def _creds(monkeypatch, w):
    w._zerochan_user = "user"
    w._zerochan_pass = "pass"


def test_gallery_dl_is_priority_when_credentials_exist(tmp_path, monkeypatch):
    w, logs = _worker(tmp_path, monkeypatch)
    _creds(monkeypatch, w)
    w.amount = 1
    calls = []

    def fake_gdl(tag, page, size):
        calls.append((page, size))
        # listing-level record: guessed name-less URL, flat tags
        return ([{"id": 3943142, "md5": "c" * 32,
                  "file_url": "https://static.zerochan.net/.full.3943142.jpg",
                  "tags": ["Female", "Touhou"]}], True)

    async def fake_json(page=1, per_page=200):
        raise AssertionError("native listing must not run while gallery-dl works")

    async def fake_page(pid):
        return POST_HTML, "https://static.zerochan.net/Remilia.Scarlet.full.3943142.png"

    _wire_scraper(monkeypatch, w, fake_json, fake_page=fake_page, fake_gdl=fake_gdl)
    asyncio.run(w.scraper_task())

    assert calls == [(1, 10)]  # constant page size keeps --range offsets aligned
    # the post page (not gallery-dl's guessed .full.N.jpg) names the file and
    # supplies the categorized tags
    assert _filenames(w) == ["Remilia.Scarlet.full.3943142.png"]
    url, _fp, _fn, _tags, artists, _size, characters, copyrights = \
        w.download_queue._queue[0][:8]
    assert url.endswith("Remilia.Scarlet.full.3943142.png")
    assert artists == ["Torinari"]
    assert characters == ["Remilia Scarlet"]
    assert copyrights == ["Touhou"]


def test_gallery_dl_short_last_page_is_end_of_tag(tmp_path, monkeypatch):
    """A short page followed by an empty range is the tag's end, not a broken
    engine — it must not switch engines or fetch a JSON page for nothing."""
    w, logs = _worker(tmp_path, monkeypatch)
    _creds(monkeypatch, w)

    def fake_gdl(tag, page, size):
        return ([{"id": 3943142}] if page == 1 else []), True

    async def fake_json(page=1, per_page=200):
        raise AssertionError("empty later range is end-of-tag, not an engine failure")

    _wire_scraper(monkeypatch, w, fake_json, fake_gdl=fake_gdl)
    asyncio.run(w.scraper_task())

    joined = "\n".join(logs)
    assert "No more posts available." in joined
    assert "switching to the built-in Zerochan API" not in joined


def test_gallery_dl_failure_falls_back_to_native(tmp_path, monkeypatch):
    w, logs = _worker(tmp_path, monkeypatch)
    _creds(monkeypatch, w)
    json_pages = []

    def fake_gdl(tag, page, size):
        return [], False  # engine broke (anon-style 500 / CF)

    async def fake_json(page=1, per_page=200):
        json_pages.append(page)
        if page == 1:
            return ([{"id": 9, "md5": "d" * 32}], True)
        return [], True

    async def fake_page(pid):
        return POST_HTML, "https://static.zerochan.net/Remilia.Scarlet.full.3943142.png"

    _wire_scraper(monkeypatch, w, fake_json, fake_page=fake_page, fake_gdl=fake_gdl)
    asyncio.run(w.scraper_task())

    assert json_pages == [1, 2]  # page 2 = real end of the tag
    assert "switching to the built-in Zerochan API" in "\n".join(logs)
    assert len(_filenames(w)) == 1


def test_gallery_dl_range_keeps_fixed_page_size(tmp_path, monkeypatch):
    """--range start/end is computed from page*size, so a size that shrinks
    with the remaining amount walks later pages back onto earlier posts."""
    w, _logs = _worker(tmp_path, monkeypatch)
    _creds(monkeypatch, w)
    w.amount = 11
    seen = []

    def fake_gdl(tag, page, size):
        seen.append((page, size))
        if page == 1:
            return [{"id": 1000 + i} for i in range(size)], True
        return [], True

    async def fake_json(page=1, per_page=200):
        return [], True

    _wire_scraper(monkeypatch, w, fake_json, fake_gdl=fake_gdl)
    asyncio.run(w.scraper_task())

    assert seen == [(1, 10), (2, 10)]


def test_gallery_dl_lists_the_tag_once_for_all_pages(tmp_path, monkeypatch):
    """One gallery-dl process lists the tag; later pages are local slices."""
    w, _logs = _worker(tmp_path, monkeypatch)
    _creds(monkeypatch, w)
    runs = []

    def fake_list(tag, limit):
        runs.append(limit)
        return [{"id": 1000 + i} for i in range(25)], True

    monkeypatch.setattr(w, "_gallery_dl_list", fake_list)
    p1, ok1 = w._gallery_dl_enumerate("Tag", 1, 10)
    p2, ok2 = w._gallery_dl_enumerate("Tag", 2, 10)
    p3, ok3 = w._gallery_dl_enumerate("Tag", 3, 10)

    assert runs == [200]
    assert (ok1, ok2, ok3) == (True, True, True)
    assert (len(p1), len(p2), len(p3)) == (10, 10, 5)
    assert p2[0]["id"] == 1010

    w2, _logs2 = _worker(tmp_path, monkeypatch)
    _creds(monkeypatch, w2)
    w2.amount = 7
    limits = []
    monkeypatch.setattr(w2, "_gallery_dl_list",
                        lambda tag, limit: (limits.append(limit) or [], True))
    w2._gallery_dl_enumerate("Tag", 1, 10)
    # the full cap is listed even for a small amount: known posts are filtered
    # out afterwards, so a shorter listing would starve the run
    assert limits == [200]


def test_gallery_dl_range_follows_the_requested_amount(tmp_path, monkeypatch):
    """A run asking for 500 posts must list 500, not stop at the old 200 cap."""
    w, logs = _worker(tmp_path, monkeypatch)
    _creds(monkeypatch, w)
    w.amount = 500
    limits = []
    posts = [{"id": 1, "file_url": "https://static.zerochan.net/Touhou.full.1.png"}]
    monkeypatch.setattr(w, "_gallery_dl_list",
                        lambda tag, limit: (limits.append(limit) or posts, True))

    async def fake_json(page=1, per_page=200):
        raise AssertionError("gallery-dl works; the API must not run")

    _wire_scraper(monkeypatch, w, fake_json, fake_gdl=w._gallery_dl_enumerate)
    asyncio.run(w.scraper_task())

    assert limits == [500]
    assert w._gdl_limit == 500
    assert "lists at most" not in "\n".join(logs)


def test_known_posts_do_not_starve_a_small_amount(tmp_path, monkeypatch):
    """5 already-downloaded images + amount=1 must fetch the 6th, not report
    the tag empty."""
    w, logs = _worker(tmp_path, monkeypatch)
    _creds(monkeypatch, w)
    w.amount = 1
    for i in range(1, 6):
        w.dl_history.add(f"Remilia.Scarlet.full.{i}.png")

    def fake_gdl(tag, page, size):
        if page == 1:
            return ([{"id": i, "file_url":
                      f"https://static.zerochan.net/Remilia.Scarlet.full.{i}.png"}
                     for i in range(1, 7)], True)
        return [], True

    async def fake_json(page=1, per_page=200):
        raise AssertionError("gallery-dl works; the API must not run")

    async def fake_page(pid):
        return POST_HTML, f"https://static.zerochan.net/Remilia.Scarlet.full.{pid}.png"

    _wire_scraper(monkeypatch, w, fake_json, fake_page=fake_page, fake_gdl=fake_gdl)
    asyncio.run(w.scraper_task())

    assert _filenames(w) == ["Remilia.Scarlet.full.6.png"]
    assert "No new images to download." not in "\n".join(logs)


def test_gallery_dl_error_record_is_engine_failure(tmp_path, monkeypatch):
    """gallery-dl -j exits 0 even on HttpError; [-1, ...] must not look like EOD."""
    w, logs = _worker(tmp_path, monkeypatch)
    _creds(monkeypatch, w)

    class _Proc:
        returncode = 0

        def communicate(self, timeout=None):
            return ('[[-1, {"error": "HttpError", "message": "\'500 Internal '
                    'Server Error\' for \'https://www.zerochan.net/Tag?json=1\'"}]]',
                    "")

        def poll(self):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr("workers.zerochan.subprocess.Popen", lambda *a, **k: _Proc())
    monkeypatch.setattr("workers.zerochan.threading.Timer",
                        lambda *a, **k: type("T", (), {"start": lambda s: None,
                                                      "cancel": lambda s: None})())
    monkeypatch.setattr(w, "_gallery_dl_launcher", lambda: ["gallery-dl"])

    posts, ok = w._gallery_dl_enumerate("Tag", 1, 10)
    assert (posts, ok) == ([], False)
    joined = "\n".join(logs)
    assert "gallery-dl listing error" in joined
    assert "500 Internal Server Error" in joined


def test_gallery_dl_without_credentials_never_runs(tmp_path, monkeypatch):
    w, _logs = _worker(tmp_path, monkeypatch)
    assert w._gallery_dl_enumerate("Tag", 1, 10) == ([], False)


def test_no_runtime_patcher_left():
    """gallery-dl is used as-is; nothing may patch its installed extractor."""
    from workers import zerochan as mod
    assert not hasattr(mod, "_ensure_gallery_dl_patch")
    try:
        import core.gallery_dl_interop  # noqa: F401
    except ImportError:
        pass
    else:
        raise AssertionError("core/gallery_dl_interop.py must stay deleted")
