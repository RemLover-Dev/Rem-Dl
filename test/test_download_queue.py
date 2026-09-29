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
        Rems_Dl._active_job = None
        Rems_Dl.DOWNLOAD_QUEUE.clear()
    yield started, gates
    # a held job can restart (and install a fresh gate) during teardown —
    # keep releasing every gate until the queue drains
    deadline = time.time() + 5
    while time.time() < deadline and not (Rems_Dl._active_job is None and not Rems_Dl.DOWNLOAD_QUEUE):
        for ev in list(gates.values()):
            ev.set()
        time.sleep(0.02)
    for ev in list(gates.values()):
        ev.set()
    assert _wait(lambda: Rems_Dl._active_job is None and not Rems_Dl.DOWNLOAD_QUEUE)
    with Rems_Dl.QUEUE_LOCK:
        Rems_Dl._active_job = None
        Rems_Dl.DOWNLOAD_QUEUE.clear()
    # ponytail: never disconnect() — it arms the 3s os._exit shutdown timer


def test_queue_serializes_one_at_a_time(queue_env):
    started, gates = queue_env
    client = Rems_Dl.socketio.test_client(Rems_Dl.app)

    client.emit("start_worker", {"worker": "gelbooru", "tag": "tag1", "net_config": {}})
    assert _wait(lambda: started == ["tag1"])

    client.emit("start_worker", {"worker": "rule34", "tag": "tag2", "net_config": {}})
    assert _wait(lambda: len(Rems_Dl.DOWNLOAD_QUEUE) == 1)
    client.emit("start_worker", {"worker": "safe", "tag": "tag3", "net_config": {}})
    assert _wait(lambda: len(Rems_Dl.DOWNLOAD_QUEUE) == 2)

    with Rems_Dl.QUEUE_LOCK:
        assert [j["tag"] for j in Rems_Dl.DOWNLOAD_QUEUE] == ["tag2", "tag3"]
    # first job still the only one running
    assert started == ["tag1"]

    gates["tag1"].set()
    assert _wait(lambda: started == ["tag1", "tag2"])
    with Rems_Dl.QUEUE_LOCK:
        assert [j["tag"] for j in Rems_Dl.DOWNLOAD_QUEUE] == ["tag3"]

    gates["tag2"].set()
    assert _wait(lambda: started == ["tag1", "tag2", "tag3"])
    gates["tag3"].set()
    assert _wait(lambda: Rems_Dl._active_job is None and not Rems_Dl.DOWNLOAD_QUEUE)

    assert "dl_queue" in [m["name"] for m in client.get_received()]


def test_stop_cancels_only_own_queued_entries(queue_env):
    started, gates = queue_env
    client = Rems_Dl.socketio.test_client(Rems_Dl.app)

    client.emit("start_worker", {"worker": "gelbooru", "tag": "run", "net_config": {}})
    assert _wait(lambda: started == ["run"])
    client.emit("start_worker", {"worker": "rule34", "tag": "wait", "net_config": {}})
    assert _wait(lambda: len(Rems_Dl.DOWNLOAD_QUEUE) == 1)

    client.emit("stop_worker", {"worker": "rule34"})
    assert _wait(lambda: len(Rems_Dl.DOWNLOAD_QUEUE) == 0)

    gates["run"].set()
    assert _wait(lambda: Rems_Dl._active_job is None)
    # the cancelled entry never ran
    assert started == ["run"]


def test_queue_move_and_cancel(queue_env):
    started, gates = queue_env
    client = Rems_Dl.socketio.test_client(Rems_Dl.app)

    client.emit("start_worker", {"worker": "gelbooru", "tag": "run", "net_config": {}})
    assert _wait(lambda: started == ["run"])
    for tag in ("a", "b", "c"):
        client.emit("start_worker", {"worker": "rule34", "tag": tag, "net_config": {}})
    assert _wait(lambda: len(Rems_Dl.DOWNLOAD_QUEUE) == 3)

    # move "c" (index 2) up to priority 1
    client.emit("queue_move", {"from": 2, "to": 0})
    with Rems_Dl.QUEUE_LOCK:
        assert [j["tag"] for j in Rems_Dl.DOWNLOAD_QUEUE] == ["c", "a", "b"]

    # out-of-range destination clamps to the end
    client.emit("queue_move", {"from": 0, "to": 99})
    with Rems_Dl.QUEUE_LOCK:
        assert [j["tag"] for j in Rems_Dl.DOWNLOAD_QUEUE] == ["a", "b", "c"]

    # cancel the middle entry
    client.emit("queue_cancel", {"index": 1})
    with Rems_Dl.QUEUE_LOCK:
        assert [j["tag"] for j in Rems_Dl.DOWNLOAD_QUEUE] == ["a", "c"]

    # invalid indices are no-ops
    client.emit("queue_cancel", {"index": 50})
    client.emit("queue_move", {"from": -1, "to": 0})
    with Rems_Dl.QUEUE_LOCK:
        assert [j["tag"] for j in Rems_Dl.DOWNLOAD_QUEUE] == ["a", "c"]

    gates["run"].set()
    assert _wait(lambda: started == ["run", "a"])
    gates["a"].set()
    assert _wait(lambda: started == ["run", "a", "c"])
    gates["c"].set()
    assert _wait(lambda: Rems_Dl._active_job is None and not Rems_Dl.DOWNLOAD_QUEUE)


def test_queue_bump_holds_active_and_starts_target(queue_env):
    started, gates = queue_env
    client = Rems_Dl.socketio.test_client(Rems_Dl.app)

    client.emit("start_worker", {"worker": "gelbooru", "tag": "first", "net_config": {}})
    assert _wait(lambda: started == ["first"])
    client.emit("start_worker", {"worker": "rule34", "tag": "second", "net_config": {}})
    client.emit("start_worker", {"worker": "safe", "tag": "third", "net_config": {}})
    assert _wait(lambda: len(Rems_Dl.DOWNLOAD_QUEUE) == 2)

    # double-click "third" (index 1): "first" goes on hold, "third" runs now
    # even though first's gate was never released
    client.emit("queue_bump", {"index": 1})
    assert _wait(lambda: started == ["first", "third"])
    with Rems_Dl.QUEUE_LOCK:
        assert Rems_Dl._active_job["tag"] == "third"
        # held job is back at the front, second still behind it
        assert [j["tag"] for j in Rems_Dl.DOWNLOAD_QUEUE] == ["first", "second"]

    gates["third"].set()
    # the held job resumes next, then second — teardown releases the tail
    assert _wait(lambda: started == ["first", "third", "first"])


def test_queue_bump_when_idle_promotes_directly(queue_env):
    started, gates = queue_env
    client = Rems_Dl.socketio.test_client(Rems_Dl.app)

    # no active job, queued entries exist (defensive path): just promote
    with Rems_Dl.QUEUE_LOCK:
        Rems_Dl.DOWNLOAD_QUEUE.append({"worker": "yande", "tag": "solo", "net_config": {}})
    client.emit("queue_bump", {"index": 0})
    assert _wait(lambda: started == ["solo"])
    gates["solo"].set()
    assert _wait(lambda: Rems_Dl._active_job is None and not Rems_Dl.DOWNLOAD_QUEUE)
