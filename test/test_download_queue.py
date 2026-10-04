import os
import threading
import time

import pytest

# Rems_Dl's load_dotenv() pollutes os.environ, which SettingsManager reads for
# its defaults — snapshot and restore so test_settings stays isolated (same
# dance as test_gallery_select, needed because this file imports Rems_Dl first).
_ENV_KEYS = [
    "API_TIMEOUT", "RETRY_WAIT", "ANTI_BAN_PAUSE", "DOWNLOAD_RETRIES",
    "USE_PROXY", "PROXY_URL", "VERIFY_TLS",
]
_env_before = {k: os.environ.get(k) for k in _ENV_KEYS}

import Rems_Dl  # noqa: E402

for _k, _v in _env_before.items():
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v


def _wait(cond, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return False


def _drained():
    return not any(Rems_Dl.ACTIVE_JOBS.values()) and not any(Rems_Dl.DOWNLOAD_QUEUES.values())


@pytest.fixture
def queue_env(monkeypatch):
    started, gates = [], {}

    def fake_dispatch(data):
        tag = data.get("tag") or data.get("category") or "?"
        started.append(tag)
        ev = threading.Event()
        gates[tag] = ev
        assert ev.wait(timeout=10), f"gate for {tag!r} never released"

    monkeypatch.setattr(Rems_Dl, "_dispatch_worker", fake_dispatch)
    with Rems_Dl.QUEUE_LOCK:
        Rems_Dl.ACTIVE_JOBS.clear()
        Rems_Dl.DOWNLOAD_QUEUES.clear()
    yield started, gates
    # a held job can restart (and install a fresh gate) during teardown —
    # keep releasing every gate until all per-site queues drain
    deadline = time.time() + 5
    while time.time() < deadline and not _drained():
        for ev in list(gates.values()):
            ev.set()
        time.sleep(0.02)
    for ev in list(gates.values()):
        ev.set()
    assert _wait(_drained)
    with Rems_Dl.QUEUE_LOCK:
        Rems_Dl.ACTIVE_JOBS.clear()
        Rems_Dl.DOWNLOAD_QUEUES.clear()
    # ponytail: never disconnect() — it arms the 3s os._exit shutdown timer


def test_queues_are_per_worker_and_serialized_within_site(queue_env):
    started, gates = queue_env
    client = Rems_Dl.socketio.test_client(Rems_Dl.app)

    client.emit("start_worker", {"worker": "gelbooru", "tag": "g1", "net_config": {}})
    assert _wait(lambda: started == ["g1"])

    # second gelbooru request queues behind the first
    client.emit("start_worker", {"worker": "gelbooru", "tag": "g2", "net_config": {}})
    assert _wait(lambda: len(Rems_Dl.DOWNLOAD_QUEUES.get("gelbooru", [])) == 1)

    # a different site runs concurrently — no global serialization
    client.emit("start_worker", {"worker": "rule34", "tag": "r1", "net_config": {}})
    assert _wait(lambda: "r1" in started)
    with Rems_Dl.QUEUE_LOCK:
        assert Rems_Dl.ACTIVE_JOBS["gelbooru"]["tag"] == "g1"
        assert Rems_Dl.ACTIVE_JOBS["rule34"]["tag"] == "r1"
        assert [j["tag"] for j in Rems_Dl.DOWNLOAD_QUEUES["gelbooru"]] == ["g2"]

    gates["g1"].set()
    assert _wait(lambda: started == ["g1", "r1", "g2"])
    with Rems_Dl.QUEUE_LOCK:
        assert not Rems_Dl.DOWNLOAD_QUEUES["gelbooru"]
    gates["r1"].set()
    gates["g2"].set()
    assert _wait(_drained)

    received = client.get_received()
    assert "dl_queue" in [m["name"] for m in received]
    payload = [m for m in received if m["name"] == "dl_queue"][-1]["args"][0]
    assert isinstance(payload["active"], list)


def test_stop_cancels_only_own_site_queue(queue_env):
    started, gates = queue_env
    client = Rems_Dl.socketio.test_client(Rems_Dl.app)

    client.emit("start_worker", {"worker": "rule34", "tag": "run", "net_config": {}})
    assert _wait(lambda: started == ["run"])
    client.emit("start_worker", {"worker": "rule34", "tag": "wait", "net_config": {}})
    assert _wait(lambda: len(Rems_Dl.DOWNLOAD_QUEUES.get("rule34", [])) == 1)

    # another site runs and queues independently
    client.emit("start_worker", {"worker": "gelbooru", "tag": "gr", "net_config": {}})
    assert _wait(lambda: "gr" in started)
    client.emit("start_worker", {"worker": "gelbooru", "tag": "gw", "net_config": {}})
    assert _wait(lambda: len(Rems_Dl.DOWNLOAD_QUEUES.get("gelbooru", [])) == 1)

    client.emit("stop_worker", {"worker": "rule34"})
    assert _wait(lambda: len(Rems_Dl.DOWNLOAD_QUEUES.get("rule34", [])) == 0)
    with Rems_Dl.QUEUE_LOCK:
        # the other site's waiting entry survives
        assert [j["tag"] for j in Rems_Dl.DOWNLOAD_QUEUES["gelbooru"]] == ["gw"]

    gates["run"].set()
    gates["gr"].set()
    # gw starts once gr hands off, then gets its own (fresh) gate
    assert _wait(lambda: "gw" in gates and started.count("gw") == 1)
    gates["gw"].set()
    assert _wait(_drained)
    # the cancelled entry never ran
    assert "wait" not in started


def test_queue_move_and_cancel(queue_env):
    started, gates = queue_env
    client = Rems_Dl.socketio.test_client(Rems_Dl.app)

    client.emit("start_worker", {"worker": "gelbooru", "tag": "run", "net_config": {}})
    assert _wait(lambda: started == ["run"])
    for tag in ("a", "b", "c"):
        client.emit("start_worker", {"worker": "gelbooru", "tag": tag, "net_config": {}})
    assert _wait(lambda: len(Rems_Dl.DOWNLOAD_QUEUES.get("gelbooru", [])) == 3)

    # move "c" (index 2) up to priority 1
    client.emit("queue_move", {"site": "gelbooru", "from": 2, "to": 0})
    with Rems_Dl.QUEUE_LOCK:
        assert [j["tag"] for j in Rems_Dl.DOWNLOAD_QUEUES["gelbooru"]] == ["c", "a", "b"]

    # out-of-range destination clamps to the end
    client.emit("queue_move", {"site": "gelbooru", "from": 0, "to": 99})
    with Rems_Dl.QUEUE_LOCK:
        assert [j["tag"] for j in Rems_Dl.DOWNLOAD_QUEUES["gelbooru"]] == ["a", "b", "c"]

    # cancel the middle entry
    client.emit("queue_cancel", {"site": "gelbooru", "index": 1})
    with Rems_Dl.QUEUE_LOCK:
        assert [j["tag"] for j in Rems_Dl.DOWNLOAD_QUEUES["gelbooru"]] == ["a", "c"]

    # invalid indices / foreign site are no-ops
    client.emit("queue_cancel", {"site": "gelbooru", "index": 50})
    client.emit("queue_move", {"site": "gelbooru", "from": -1, "to": 0})
    client.emit("queue_move", {"site": "rule34", "from": 0, "to": 0})
    with Rems_Dl.QUEUE_LOCK:
        assert [j["tag"] for j in Rems_Dl.DOWNLOAD_QUEUES["gelbooru"]] == ["a", "c"]

    gates["run"].set()
    assert _wait(lambda: started == ["run", "a"])
    gates["a"].set()
    assert _wait(lambda: started == ["run", "a", "c"])
    gates["c"].set()
    assert _wait(_drained)


def test_queue_bump_holds_active_and_starts_target(queue_env):
    started, gates = queue_env
    client = Rems_Dl.socketio.test_client(Rems_Dl.app)

    client.emit("start_worker", {"worker": "gelbooru", "tag": "first", "net_config": {}})
    assert _wait(lambda: started == ["first"])
    client.emit("start_worker", {"worker": "gelbooru", "tag": "second", "net_config": {}})
    client.emit("start_worker", {"worker": "gelbooru", "tag": "third", "net_config": {}})
    assert _wait(lambda: len(Rems_Dl.DOWNLOAD_QUEUES.get("gelbooru", [])) == 2)

    # another site's job keeps running untouched through the bump
    client.emit("start_worker", {"worker": "rule34", "tag": "other", "net_config": {}})
    assert _wait(lambda: "other" in started)

    # double-click "third" (index 1): "first" goes on hold, "third" runs now
    # even though first's gate was never released
    client.emit("queue_bump", {"site": "gelbooru", "index": 1})
    assert _wait(lambda: started == ["first", "third", "other"] or started[-1] == "third")
    with Rems_Dl.QUEUE_LOCK:
        assert Rems_Dl.ACTIVE_JOBS["gelbooru"]["tag"] == "third"
        # held job is back at the front, second still behind it
        assert [j["tag"] for j in Rems_Dl.DOWNLOAD_QUEUES["gelbooru"]] == ["first", "second"]
        assert Rems_Dl.ACTIVE_JOBS["rule34"]["tag"] == "other"

    gates["third"].set()
    # the held job resumes next, then second — teardown releases the tail
    assert _wait(lambda: started[-1] == "first" and started.count("first") == 2)
    gates["other"].set()


def test_queue_bump_when_idle_promotes_directly(queue_env):
    started, gates = queue_env
    client = Rems_Dl.socketio.test_client(Rems_Dl.app)

    # no active job for the site, waiting entries exist (defensive path):
    # just promote
    with Rems_Dl.QUEUE_LOCK:
        Rems_Dl.DOWNLOAD_QUEUES.setdefault("yande", []).append(
            {"worker": "yande", "tag": "solo", "net_config": {}})
    client.emit("queue_bump", {"site": "yande", "index": 0})
    assert _wait(lambda: started == ["solo"])
    gates["solo"].set()
    assert _wait(_drained)


def test_queue_add_defers_until_start_worker(queue_env):
    started, gates = queue_env
    client = Rems_Dl.socketio.test_client(Rems_Dl.app)

    # queue while idle: entries wait, nothing runs yet
    for tag in ("q1", "q2"):
        client.emit("queue_add", {"worker": "gelbooru", "tag": tag, "net_config": {}})
    assert _wait(lambda: len(Rems_Dl.DOWNLOAD_QUEUES.get("gelbooru", [])) == 2)
    assert started == []

    # START kicks off the head; a payload-less start never runs the payload
    client.emit("start_worker", {"worker": "gelbooru"})
    assert _wait(lambda: started == ["q1"])
    with Rems_Dl.QUEUE_LOCK:
        assert [j["tag"] for j in Rems_Dl.DOWNLOAD_QUEUES["gelbooru"]] == ["q2"]

    gates["q1"].set()
    assert _wait(lambda: started == ["q1", "q2"])
    gates["q2"].set()
    assert _wait(_drained)

    # payload-less start with nothing queued is a no-op
    client.emit("start_worker", {"worker": "gelbooru"})
    assert started == ["q1", "q2"]


def test_start_worker_with_queue_joins_back(queue_env):
    started, gates = queue_env
    client = Rems_Dl.socketio.test_client(Rems_Dl.app)

    client.emit("queue_add", {"worker": "gelbooru", "tag": "head", "net_config": {}})
    client.emit("start_worker", {"worker": "gelbooru", "tag": "form", "net_config": {}})
    # the queued head runs first; the fresh form payload joins the back
    assert _wait(lambda: started == ["head"])
    with Rems_Dl.QUEUE_LOCK:
        assert [j["tag"] for j in Rems_Dl.DOWNLOAD_QUEUES["gelbooru"]] == ["form"]

    gates["head"].set()
    assert _wait(lambda: started == ["head", "form"])
    gates["form"].set()
    assert _wait(_drained)
