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
    monkeypatch.setattr("workers.gelbooru.TAG_TYPES_FILE", str(tmp_path / "types.json"))

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


def test_pace_spaces_request_starts_under_isp_limit(monkeypatch):
    """Every worker shares one rate budget (Settings → req_rate_limit,
    default 4 req/s) — the old 32-way tag fan-out burst exceeded the ISP
    cap and the failures left tags uncategorized after 3 retries."""
    import core.shared as shared

    monkeypatch.setenv("REQ_RATE_LIMIT", "4")

    async def _fast(_delay):
        return None

    monkeypatch.setattr(shared.asyncio, "sleep", _fast)

    async def run():
        await shared._pace_on_request_start(None, None, None)  # prime: move slot into the future
        before = shared._pace_next
        for _ in range(5):
            await shared._pace_on_request_start(None, None, None)
        return shared._pace_next - before

    assert abs(asyncio.run(run()) - 5 * 0.25) < 1e-6


def test_pace_session_paces_request_funnel(monkeypatch):
    """Sync workers (rule34 lib, eshuushuu, pixiv, pinterest-dl) pace via
    session.request — every verb funnels through it, once."""
    import core.shared as shared

    monkeypatch.setenv("REQ_RATE_LIMIT", "4")
    monkeypatch.setattr(shared.time, "sleep", lambda _s: None)

    class _S:
        def request(self, *a, **k):
            return "ok"

    s = shared.pace_session(_S())
    assert s.request("GET", "prime") == "ok"  # first slot lands on `now`
    before = shared._pace_next
    assert s.request("POST", "u") == "ok"
    assert s.request("GET", "u") == "ok"
    assert abs((shared._pace_next - before) - 0.5) < 1e-3
    assert shared.pace_session(s) is s  # idempotent — no double pace


def test_base_session_wires_rate_pace(tmp_path, monkeypatch):
    """The aiohttp trace hook is what paces tag fetches, page scans and
    downloads — one insertion covers every worker session."""
    w = _worker(tmp_path, monkeypatch)

    async def _mk():
        s = await w._create_session()
        cfgs = list(s._trace_configs)
        await s.close()
        return cfgs

    cfgs = asyncio.run(_mk())
    assert cfgs
    assert len(cfgs[0].on_request_start) == 1
