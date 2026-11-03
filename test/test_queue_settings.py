import os
import time

import pytest

# Rems_Dl's load_dotenv() pollutes os.environ, which SettingsManager reads for
# its defaults — snapshot and restore (same dance as test_download_queue).
_ENV_KEYS = [
    "API_TIMEOUT", "RETRY_WAIT", "ANTI_BAN_PAUSE", "DOWNLOAD_RETRIES",
    "USE_PROXY", "PROXY_URL", "VERIFY_TLS",
]
_env_before = {k: os.environ.get(k) for k in _ENV_KEYS}

import Rems_Dl

for _k, _v in _env_before.items():
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v

from core import shared


def _wait(cond, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return False


def _settled():
    # the finally block resolves active+queue together under QUEUE_LOCK, so
    # an empty ACTIVE_JOBS means the run is over
    return not Rems_Dl.ACTIVE_JOBS


@pytest.fixture
def queue_env(monkeypatch):
    dispatches = []

    def fake_dispatch(data):
        dispatches.append(dict(data))

    monkeypatch.setattr(Rems_Dl, "_dispatch_worker", fake_dispatch)
    monkeypatch.setattr(shared, "log_msg", lambda *a, **k: None)
    monkeypatch.setattr(shared, "recategorize_all", lambda: (0, 0))
    with Rems_Dl.QUEUE_LOCK:
        Rems_Dl.ACTIVE_JOBS.clear()
        Rems_Dl.DOWNLOAD_QUEUES.clear()
    yield dispatches
    with Rems_Dl.QUEUE_LOCK:
        Rems_Dl.ACTIVE_JOBS.clear()
        Rems_Dl.DOWNLOAD_QUEUES.clear()


def test_start_applies_form_settings_to_queued_jobs(queue_env):
    dispatches = queue_env
    Rems_Dl.handle_queue_add({"worker": "gelbooru", "tag": "a", "limit": "50", "rating": "g"})
    Rems_Dl.handle_queue_add({"worker": "gelbooru", "tag": "b", "limit": "50", "rating": "g"})
    # limit + rating typed AFTER queueing, then START
    Rems_Dl.handle_start_worker({"worker": "gelbooru", "tag": "c", "limit": "2", "rating": "safe"})
    assert _wait(lambda: len(dispatches) == 3 and _settled()), "run never settled"
    assert [d["limit"] for d in dispatches] == ["2", "2", "2"]
    assert [d["rating"] for d in dispatches] == ["safe", "safe", "safe"]
    # identity stays per-job — the form never renames a queued tag
    assert [d["tag"] for d in dispatches] == ["a", "b", "c"]


def test_limit_is_per_tag_not_a_total_cap(queue_env):
    # limit 2 with several tags = 2 images from EACH tag (runs all of them)
    dispatches = queue_env
    for t in ("a", "b", "c"):
        Rems_Dl.handle_queue_add({"worker": "dan", "tag": t, "limit": "50"})
    Rems_Dl.handle_start_worker({"worker": "dan", "tag": "d", "limit": "2"})
    assert _wait(lambda: len(dispatches) == 4 and _settled()), "run never settled"
    assert [d["limit"] for d in dispatches] == ["2", "2", "2", "2"]


def test_settings_propagate_only_to_the_started_site(queue_env):
    dispatches = queue_env
    Rems_Dl.handle_queue_add({"worker": "gelbooru", "tag": "a", "limit": "50", "rating": "g"})
    Rems_Dl.handle_start_worker({"worker": "dan", "tag": "x", "limit": "2", "rating": "q"})
    assert _wait(lambda: len(dispatches) == 1 and _settled()), "run never settled"
    assert dispatches[0]["tag"] == "x"
    # the other site's waiting job keeps its own snapshot
    waiting = Rems_Dl.DOWNLOAD_QUEUES["gelbooru"][0]
    assert waiting["limit"] == "50"
    assert waiting["rating"] == "g"


def test_net_config_stays_per_job(queue_env):
    dispatches = queue_env
    Rems_Dl.handle_queue_add({"worker": "gelbooru", "tag": "a", "limit": "50",
                              "net_config": {"api_timeout": "7"}})
    Rems_Dl.handle_start_worker({"worker": "gelbooru", "tag": "c", "limit": "5",
                                 "net_config": {"api_timeout": "99"}})
    assert _wait(lambda: len(dispatches) == 2 and _settled()), "run never settled"
    # queued job's own net_config survives; limit still propagates
    assert dispatches[0]["net_config"] == {"api_timeout": "7"}
    assert dispatches[0]["limit"] == "5"
