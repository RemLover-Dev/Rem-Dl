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
