"""Folder-name-only gallery entries are recovered by md5 lookup.

A folder rescan can only ever know a file's path, so those entries carry one
tag (the folder name, which recategorize then reads as the artist). The md5
in the filename identifies the post, and the API hands the real tags back —
but only for a post that proves it is ours.
"""
import hashlib

import pytest

from core import md5_refetch as refetch
from core import shared

MD5 = "c507b1418f8e675857b4f5bd982b9d6e"
CACHES = {"gelbooru": {"someartist_(x)": "artist", "somechar_(y)": "character"}}


def _entry(site="gelbooru", filename=f"{MD5}.png", tags=None):
    return {"site": site, "filename": filename,
            "filepath": f"{site}/artist/Safe/images/{filename}",
            "tags": tags if tags is not None else {"tag": [], "artist": ["artist_folder"]}}


def test_is_bare_is_the_rescan_signature():
    assert refetch.is_bare({"tags": {"tag": [], "artist": ["hiten_(hitenkei)"]}})
    assert refetch.is_bare({"tags": {"tag": ["x"]}})
    assert refetch.is_bare({"tags": None})
    assert not refetch.is_bare({"tags": {"artist": ["a"], "tag": ["1girl"]}})


def test_supports_covers_only_sites_with_an_md5_lookup(monkeypatch):
    assert refetch.supports("gelbooru")
    assert refetch.supports("danbooru")
    # site aliases normalize before the lookup
    assert refetch.supports("yande.re")
    # no md5 search — never asked for anything
    assert not refetch.supports("waifu.im")
    assert not refetch.supports("zero")          # -> zerochan
    assert not refetch.supports("eshuushuu")
    # gsbooru's API has no md5 search at all — never asked for anything
    # (GSBOORU_API_KEY is irrelevant: tags=md5: and md5= are both ignored)
    monkeypatch.delenv("GSBOORU_API_KEY", raising=False)
    assert not refetch.supports("gsbooru")
    monkeypatch.setenv("GSBOORU_API_KEY", "k")
    assert not refetch.supports("gsbooru")


def test_refetch_replaces_folder_name_with_real_tags(monkeypatch):
    payload = {"post": [{"md5": MD5, "tags": "someartist_(x) somechar_(y) blue_hair"}]}
    calls = []
    monkeypatch.setattr(refetch, "_get",
                        lambda url, params, bearer=None: calls.append(params) or payload)
    entry = _entry()

    stats = refetch.refetch_entries([entry], caches=CACHES, embed=False)

    assert stats["refetched"] == 1
    assert len(calls) == 1
    assert "md5:" in calls[0]["tags"]
    # flat response is categorized from the site cache; folder name is gone
    assert entry["tags"]["artist"] == ["someartist_(x)"]
    assert entry["tags"]["character"] == ["somechar_(y)"]
    assert entry["tags"]["tag"] == ["blue_hair"]


def test_a_post_that_is_not_ours_is_rejected(monkeypatch):
    """A site that ignores md5: must never lend its tags to our file."""
    monkeypatch.setattr(refetch, "_get",
                        lambda url, params, bearer=None: {"post": [{"md5": "0" * 32, "tags": "wrong"}]})
    entry = _entry()

    stats = refetch.refetch_entries([entry], caches=CACHES, embed=False)

    assert stats["not_found"] == 1
    assert entry["tags"] == {"tag": [], "artist": ["artist_folder"]}


def test_unsupported_site_never_touches_the_network(monkeypatch):
    monkeypatch.setattr(refetch, "_get",
                        lambda *a, **k: pytest.fail("unsupported site must not fetch"))
    stats = refetch.refetch_entries([_entry(site="waifu.im")], caches={})
    assert stats["unsupported"] == 1


def test_danbooru_buckets_come_from_the_post_itself(monkeypatch, tmp_path):
    monkeypatch.setattr(shared, "MASTER_FOLDER", str(tmp_path))
    content = b"not-a-real-image"
    md5 = hashlib.md5(content).hexdigest()
    (tmp_path / "Dan").mkdir(parents=True)
    (tmp_path / "Dan" / "dl.jpg").write_bytes(content)
    entry = _entry(site="danbooru", filename="dl.jpg")
    entry["filepath"] = "Dan/dl.jpg"
    monkeypatch.setattr(refetch, "_get", lambda url, params, bearer=None: [{
        "md5": md5, "tag_string_artist": "artist_a", "tag_string_character": "char_a",
        "tag_string_copyright": "copy_a", "tag_string_meta": "meta_a",
        "tag_string_general": "g1 g2"}])

    stats = refetch.refetch_entries([entry], embed=False)

    assert stats["refetched"] == 1
    # categories come from the post itself, not from a site tag cache
    assert entry["tags"]["artist"] == ["artist_a"]
    assert entry["tags"]["character"] == ["char_a"]
    assert entry["tags"]["copyright"] == ["copy_a"]
    assert entry["tags"]["metadata"] == ["meta_a"]
    assert entry["tags"]["tag"] == ["g1", "g2"]


def test_recovered_tags_are_embedded_in_the_file(monkeypatch, tmp_path):
    monkeypatch.setattr(shared, "MASTER_FOLDER", str(tmp_path))
    (tmp_path / "Gelbooru").mkdir(parents=True)
    path = tmp_path / "Gelbooru" / f"{MD5}.jpg"
    path.write_bytes(b"\xff\xd8" + b"\x00" * 64)
    monkeypatch.setattr(refetch, "_get", lambda url, params, bearer=None: {
        "post": [{"md5": MD5, "tags": "someartist_(x) blue_hair"}]})
    entry = _entry(filename=f"{MD5}.jpg")
    entry["filepath"] = f"Gelbooru/{MD5}.jpg"

    refetch.refetch_entries([entry], caches=CACHES)

    meta = shared.read_image_metadata(str(path))
    assert meta and "artist:someartist_(x)" in meta
    assert "tag:blue_hair" in meta


def test_a_second_miss_retires_the_entry(monkeypatch):
    """An image the API denies is not re-asked on every single launch."""
    monkeypatch.setattr(refetch, "_get", lambda url, params, bearer=None: {"post": []})
    entry = _entry()
    assert refetch.worth_trying(entry)

    refetch.refetch_entries([entry], caches=CACHES, embed=False)
    assert entry["md5_misses"] == 1
    assert refetch.worth_trying(entry)          # one blip is not a verdict

    refetch.refetch_entries([entry], caches=CACHES, embed=False)
    assert entry["md5_misses"] == 2
    assert not refetch.worth_trying(entry)      # never asked again


def test_dry_run_records_no_miss(monkeypatch):
    monkeypatch.setattr(refetch, "_get", lambda url, params, bearer=None: {"post": []})
    entry = _entry()

    refetch.refetch_entries([entry], dry=True, caches=CACHES, embed=False)

    assert "md5_misses" not in entry
    assert refetch.worth_trying(entry)


def test_a_recovered_entry_clears_its_miss_counter(monkeypatch):
    monkeypatch.setattr(refetch, "_get", lambda url, params, bearer=None: {
        "post": [{"md5": MD5, "tags": "someartist_(x) blue_hair"}]})
    entry = _entry()
    entry["md5_misses"] = 1

    refetch.refetch_entries([entry], caches=CACHES, embed=False)

    assert "md5_misses" not in entry
    assert entry["tags"]["artist"] == ["someartist_(x)"]


def test_worth_trying_gates_on_site_and_bareness():
    assert refetch.worth_trying(_entry())
    assert not refetch.worth_trying(_entry(site="waifu.im"))
    assert not refetch.worth_trying(_entry(tags={"artist": ["a"], "tag": ["1girl"]}))


def _echo_get(url, params, bearer=None):
    """Answer with our own post for whatever md5 was asked."""
    return {"post": [{"md5": params["tags"].split("md5:", 1)[1],
                      "tags": "someartist_(x) blue_hair"}]}


def _entries(n):
    return [_entry(filename=f"{i:032x}.png") for i in range(n)]


def test_progress_is_checkpointed_so_a_shutdown_loses_at_most_ten(monkeypatch):
    monkeypatch.setattr(refetch, "_get", _echo_get)
    saves = []

    refetch.refetch_entries(_entries(12), caches=CACHES, embed=False,
                            checkpoint=lambda: saves.append(1))

    assert len(saves) == 2            # every 10, plus the one on the way out


def test_ctrl_c_keeps_what_was_already_fetched(monkeypatch):
    monkeypatch.setattr(refetch, "_get", _echo_get)
    seen, saves = [], []
    calls = {"n": 0}

    def interrupt_on_third(url, params, bearer=None):
        calls["n"] += 1
        if calls["n"] == 3:
            raise KeyboardInterrupt
        return _echo_get(url, params, bearer)

    monkeypatch.setattr(refetch, "_get", interrupt_on_third)

    stats = refetch.refetch_entries(_entries(5), caches=CACHES, embed=False,
                                    progress=seen.append,
                                    checkpoint=lambda: saves.append(1))

    assert stats["refetched"] == 2            # the two that completed
    assert saves == [1]                       # flushed before returning
    assert any("interrupted" in line for line in seen)


def test_dry_run_never_writes_the_gallery(monkeypatch):
    monkeypatch.setattr(refetch, "_get", _echo_get)
    saves = []

    stats = refetch.refetch_entries(_entries(3), dry=True, caches=CACHES,
                                    embed=False, checkpoint=lambda: saves.append(1))

    assert stats["refetched"] == 3
    assert saves == []


def test_ctrl_c_stops_after_the_current_entry(monkeypatch):
    """curl_cffi prints ^C as "Exception ignored" and swallows it — a flag
    has to do the stopping, and the work already done has to survive it."""
    seen = []

    def interrupt_mid_request(url, params, bearer=None):
        refetch.STOP = True                      # ^C lands during request 1
        return _echo_get(url, params, bearer)

    monkeypatch.setattr(refetch, "_get", interrupt_mid_request)
    monkeypatch.setattr(refetch, "STOP", False)

    stats = refetch.refetch_entries(_entries(5), caches=CACHES, embed=False,
                                    progress=seen.append,
                                    checkpoint=lambda: seen.append("SAVE"))

    assert stats["refetched"] == 1               # only the in-flight one
    assert any("interrupted" in line for line in seen)
    assert seen.count("SAVE") == 1               # flushed on the way out


def test_a_ctrl_c_takedown_does_not_condemn_the_host(monkeypatch):
    monkeypatch.setattr(refetch, "STOP", False)
    refetch._mark_dead("example.com")
    assert "example.com" in refetch._dead_hosts
    refetch._dead_hosts.discard("example.com")

    monkeypatch.setattr(refetch, "STOP", True)
    with pytest.raises(KeyboardInterrupt):
        refetch._mark_dead("yande.re")
    assert "yande.re" not in refetch._dead_hosts  # healthy site stays usable


def test_dry_run_reports_but_changes_nothing(monkeypatch):
    monkeypatch.setattr(refetch, "_get", lambda url, params, bearer=None: {
        "post": [{"md5": MD5, "tags": "someartist_(x) blue_hair"}]})
    entry = _entry()

    stats = refetch.refetch_entries([entry], dry=True, caches=CACHES, embed=False)

    assert stats["refetched"] == 1
    assert entry["tags"] == {"tag": [], "artist": ["artist_folder"]}


def test_dead_host_short_circuits_before_any_request():
    refetch._dead_hosts.add("gelbooru.com")
    try:
        with pytest.raises(refetch.HostDead):
            refetch._get("https://gelbooru.com/index.php", {})
    finally:
        refetch._dead_hosts.discard("gelbooru.com")


def _fake_curl(monkeypatch, outcomes):
    """Record the proxy of each attempt; outcomes maps proxy -> Exception|response."""
    import curl_cffi.requests as cc_requests

    seen = []

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"post": []}

    def fake_get(url, **kwargs):
        px = kwargs.get("proxy")
        seen.append(px)
        outcome = outcomes.get(px, _Resp())
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(cc_requests, "get", fake_get)
    return seen


def test_danbooru_is_asked_as_an_api_client_not_a_browser(monkeypatch):
    """Browser spoofing and curl's default UA both get a Cloudflare
    challenge — only an honest app UA with the key gets a 200."""
    import curl_cffi.requests as cc_requests
    seen = {}

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"post": []}

    def fake_get(url, **kw):
        seen.clear()
        seen.update(kw)
        return _Resp()

    monkeypatch.setattr(cc_requests, "get", fake_get)
    monkeypatch.delenv("USE_PROXY", raising=False)
    refetch._dead_hosts.discard("danbooru.donmai.us")

    refetch._get("https://danbooru.donmai.us/posts.json", {})

    assert seen.get("impersonate") is None
    assert seen["headers"].get("User-Agent") == refetch._API_UA

    # every other site still rides the browser impersonation that works for it
    refetch._get("https://gelbooru.com/index.php", {})
    assert seen.get("impersonate") == "chrome"
    assert "User-Agent" not in seen["headers"]


def test_without_a_proxy_the_direct_route_still_runs(monkeypatch):
    """An unset proxy used to leave zero attempts — every lookup 'failed'."""
    monkeypatch.delenv("USE_PROXY", raising=False)
    monkeypatch.delenv("PROXY_URL", raising=False)
    seen = _fake_curl(monkeypatch, {})

    refetch._get("https://gelbooru.com/index.php", {})

    assert seen == [None]


def test_proxy_route_falls_back_to_direct(monkeypatch):
    monkeypatch.setenv("USE_PROXY", "true")
    monkeypatch.setenv("PROXY_URL", "http://127.0.0.1:10808")
    seen = _fake_curl(monkeypatch, {"http://127.0.0.1:10808": RuntimeError("tls")})
    host = "danbooru.donmai.us"
    refetch._dead_hosts.discard(host)

    refetch._get(f"https://{host}/posts.json", {})

    assert seen == ["http://127.0.0.1:10808", None]
    refetch._dead_hosts.discard(host)


def test_danbooru_filenames_are_post_ids_not_md5s():
    assert refetch._danbooru_id({"filename": "12038425.jpg"}) == "12038425"
    assert refetch._danbooru_id({"filename": f"{MD5}.png"}) is None
    assert refetch._danbooru_id({"filename": "dl.jpg"}) is None
    _url, params, _kind = refetch._query("danbooru", MD5, post_id="12038425")
    assert params["tags"] == "id:12038425"
    _url, params, _kind = refetch._query("danbooru", MD5)
    assert params["tags"] == f"md5:{MD5}"


def test_a_post_with_the_wrong_id_never_lends_its_tags(monkeypatch):
    """Identity for a numeric filename is the id — never someone else's post."""
    monkeypatch.setattr(refetch, "_get", lambda url, params, bearer=None: [
        {"id": 999, "md5": "f" * 32, "tag_string_artist": "impostor"}])
    entry = _entry(site="danbooru", filename="12038425.jpg")

    stats = refetch.refetch_entries([entry], caches=CACHES, embed=False)

    assert stats["not_found"] == 1
    assert entry["tags"] == {"tag": [], "artist": ["artist_folder"]}


def _dan_post(post_id, md5):
    return {"id": post_id, "md5": md5,
            "file_url": f"https://cdn.donmai.us/original/{md5[:2]}/{md5[2:4]}/{md5}.jpg",
            "tag_string_artist": "artist_a", "tag_string_character": "char_a",
            "tag_string_copyright": "copy_a", "tag_string_meta": "meta_a",
            "tag_string_general": "g1 g2"}


def _fullsize_setup(tmp_path, monkeypatch, content):
    monkeypatch.setattr(shared, "MASTER_FOLDER", str(tmp_path))
    (tmp_path / "Dan").mkdir(parents=True)
    path = tmp_path / "Dan" / "12038425.jpg"
    path.write_bytes(content)
    entry = _entry(site="danbooru", filename="12038425.jpg")
    entry["filepath"] = "Dan/12038425.jpg"
    return entry, path


def test_fullsize_swaps_a_reencode_for_the_original(tmp_path, monkeypatch):
    entry, path = _fullsize_setup(tmp_path, monkeypatch, b"re-encoded copy")
    fresh = b"original bytes from danbooru"
    post = _dan_post(12038425, hashlib.md5(fresh).hexdigest())
    fetched = []

    def fake_get(url, params, bearer=None, parse="json", timeout=20):
        if parse == "content":
            fetched.append(url)
            return fresh
        return [post]

    monkeypatch.setattr(refetch, "_get", fake_get)

    stats = refetch.refetch_entries([entry], caches=CACHES, embed=False,
                                    fullsize=True)

    assert stats["refetched"] == 1
    assert stats["fullsize"] == 1
    assert len(fetched) == 1
    assert path.read_bytes() == fresh
    assert entry["tags"]["artist"] == ["artist_a"]
    assert not (path.parent / (path.name + ".fullsize")).exists()


def test_fullsize_leaves_an_already_original_file_alone(tmp_path, monkeypatch):
    fresh = b"original bytes from danbooru"
    entry, path = _fullsize_setup(tmp_path, monkeypatch, fresh)
    post = _dan_post(12038425, hashlib.md5(fresh).hexdigest())

    def fake_get(url, params, bearer=None, parse="json", timeout=20):
        assert parse != "content"          # bytes never requested
        return [post]

    monkeypatch.setattr(refetch, "_get", fake_get)

    stats = refetch.refetch_entries([entry], caches=CACHES, embed=False,
                                    fullsize=True)

    assert stats["refetched"] == 1
    assert "fullsize" not in stats
    assert path.read_bytes() == fresh


def test_fullsize_dry_run_never_touches_the_bytes(tmp_path, monkeypatch):
    stale = b"re-encoded copy"
    entry, path = _fullsize_setup(tmp_path, monkeypatch, stale)
    post = _dan_post(12038425, hashlib.md5(b"original bytes").hexdigest())

    def fake_get(url, params, bearer=None, parse="json", timeout=20):
        assert parse != "content"
        return [post]

    monkeypatch.setattr(refetch, "_get", fake_get)

    stats = refetch.refetch_entries([entry], dry=True, caches=CACHES,
                                    embed=False, fullsize=True)

    assert stats["refetched"] == 1
    assert "fullsize" not in stats
    assert path.read_bytes() == stale


def test_a_degraded_download_never_overwrites_the_file(tmp_path, monkeypatch):
    stale = b"re-encoded copy"
    entry, path = _fullsize_setup(tmp_path, monkeypatch, stale)
    claimed = hashlib.md5(b"the real original").hexdigest()
    post = _dan_post(12038425, claimed)

    def fake_get(url, params, bearer=None, parse="json", timeout=20):
        if parse == "content":
            return b"what the CDN actually sent"   # md5 != claimed
        return [post]

    monkeypatch.setattr(refetch, "_get", fake_get)

    stats = refetch.refetch_entries([entry], caches=CACHES, embed=False,
                                    fullsize=True)

    assert stats["refetched"] == 1
    assert "fullsize" not in stats
    assert path.read_bytes() == stale


def test_a_missing_post_retires_only_bare_candidates(monkeypatch):
    monkeypatch.setattr(refetch, "_get", lambda url, params, bearer=None: [])
    tagged = _entry(tags={"artist": ["a"], "tag": ["1girl"]})
    bare = _entry(filename=f"{'d' * 32}.png")

    stats = refetch.refetch_entries([tagged, bare], caches=CACHES, embed=False)

    assert stats["not_found"] == 2
    assert "md5_misses" not in tagged     # healthy tagged entry never flagged
    assert bare["md5_misses"] == 1


def test_stale_md5_misses_are_dropped_where_the_query_could_never_match():
    """danbooru's md5-era verdicts retire entries id: would find; gsbooru's
    come from a search its API does not implement, so both are meaningless."""
    retired = _entry(site="danbooru", filename="12038425.jpg")
    retired["md5_misses"] = 2
    gs = _entry(site="gsbooru")
    gs["md5_misses"] = 2
    keep = _entry(site="gelbooru")
    keep["md5_misses"] = 2

    dropped = refetch.clear_stale_md5_misses([retired, gs, keep])

    assert dropped == 2
    assert "md5_misses" not in retired
    assert "md5_misses" not in gs
    assert refetch.worth_trying(retired)
    assert not refetch.worth_trying(gs)         # unsupported either way
    assert keep["md5_misses"] == 2              # a real md5 verdict stands
    assert not refetch.worth_trying(keep)


def _resp(monkeypatch, content):
    """Feed _get a fixed 200 response whose json() chokes like curl_cffi's."""
    import curl_cffi.requests as cc_requests

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            raise ValueError("Expecting value: line 1 column 1 (char 0)")

    _Resp.content = content

    monkeypatch.setattr(cc_requests, "get", lambda url, **kw: _Resp())
    monkeypatch.delenv("USE_PROXY", raising=False)
    return _Resp


def test_an_empty_body_is_no_posts_not_a_dead_host(monkeypatch):
    """safebooru answers a query with no hits with an empty 200 body."""
    _resp(monkeypatch, b"")
    refetch._dead_hosts.discard("safebooru.org")

    assert refetch._get("https://safebooru.org/index.php", {}) == []
    assert "safebooru.org" not in refetch._dead_hosts


def test_a_garbage_body_still_condemns_the_host(monkeypatch):
    """An HTML challenge page is a failure — cutting the host off is right."""
    _resp(monkeypatch, b"<!DOCTYPE html><html>Just a moment...</html>")
    refetch._dead_hosts.discard("gelbooru.com")

    with pytest.raises(refetch.HostDead):
        refetch._get("https://gelbooru.com/index.php", {})
    assert "gelbooru.com" in refetch._dead_hosts
    refetch._dead_hosts.discard("gelbooru.com")


def test_safebooru_calls_its_md5_field_hash(monkeypatch):
    monkeypatch.setattr(refetch, "_get", lambda url, params: [
        {"hash": MD5, "tags": "someartist_(x) blue_hair"}])
    entry = _entry(site="safebooru")

    stats = refetch.refetch_entries([entry], caches=CACHES, embed=False)

    assert stats["refetched"] == 1
    assert entry["tags"]["tag"] == ["someartist_(x)", "blue_hair"]
