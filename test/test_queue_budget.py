import os
import threading
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


@pytest.fixture
def budget_env(monkeypatch):
    dispatches = []

    def fake_dispatch(data):
        site = data.get("worker") or "?"
        # limits arrive as strings from the form — same as production
        got = int(data.get("limit") or 0)
        dispatches.append((site, got))
        # pretend the worker fetched exactly `limit` images
        b = shared._BATCH_STATS.setdefault(site, {})
        b["downloaded"] = b.get("downloaded", 0) + got

    monkeypatch.setattr(Rems_Dl, "_dispatch_worker", fake_dispatch)
    monkeypatch.setattr(shared, "log_msg", lambda *a, **k: None)
    monkeypatch.setattr(shared, "recategorize_all", lambda: (0, 0))
    monkeypatch.setattr(shared, "_BATCH_STATS", {})
    with Rems_Dl.QUEUE_LOCK:
        Rems_Dl.ACTIVE_JOBS.clear()
        Rems_Dl.DOWNLOAD_QUEUES.clear()
        Rems_Dl.RUN_BUDGETS.clear()
    yield dispatches
    with Rems_Dl.QUEUE_LOCK:
        Rems_Dl.ACTIVE_JOBS.clear()
        Rems_Dl.DOWNLOAD_QUEUES.clear()
        Rems_Dl.RUN_BUDGETS.clear()


def _drained():
    # a pass through the finally block resolves active+queue+budget together
    # under QUEUE_LOCK, so an empty ACTIVE_JOBS means the run is over
    return not Rems_Dl.ACTIVE_JOBS


def test_run_budget_caps_whole_queue_not_each_tag(budget_env):
    dispatches = budget_env
    # b and c queued first; START (tag a, limit 2) joins the back and sets
    # the run budget — whichever tag runs first takes 2 images TOTAL and the
    # rest of the queue keeps waiting
    Rems_Dl.handle_queue_add({"worker": "gelbooru", "tag": "b", "limit": "5"})
    Rems_Dl.handle_queue_add({"worker": "gelbooru", "tag": "c", "limit": "5"})
    Rems_Dl.handle_start_worker({"worker": "gelbooru", "tag": "a", "limit": "2"})
    assert _wait(lambda: len(dispatches) == 1 and _drained()), "run never settled"
    # head tag b ran clamped to the 2-image budget; nobody after it
    assert dispatches == [("gelbooru", 2)]
    assert [e["tag"] for e in Rems_Dl.DOWNLOAD_QUEUES["gelbooru"]] == ["c", "a"]
    assert "gelbooru" not in Rems_Dl.RUN_BUDGETS


def test_run_budget_clamps_each_job_to_remainder(budget_env):
    dispatches = budget_env
    Rems_Dl.handle_queue_add({"worker": "gelbooru", "tag": "b", "limit": "3"})
    Rems_Dl.handle_queue_add({"worker": "gelbooru", "tag": "c", "limit": "10"})
    Rems_Dl.handle_start_worker({"worker": "gelbooru", "tag": "a", "limit": "9"})
    assert _wait(lambda: len(dispatches) == 2 and _drained()), "run never settled"
    # budget 9: b takes 3, c wants 10 but only 6 remain — clamped, then stop
    assert dispatches == [("gelbooru", 3), ("gelbooru", 6)]
    assert [e["tag"] for e in Rems_Dl.DOWNLOAD_QUEUES["gelbooru"]] == ["a"]
    assert "gelbooru" not in Rems_Dl.RUN_BUDGETS


def test_no_budget_keeps_per_tag_semantics(budget_env):
    # START without a typed payload (queue-only kick) = old behaviour
    dispatches = budget_env
    Rems_Dl.handle_queue_add({"worker": "gelbooru", "tag": "a", "limit": "50"})
    Rems_Dl.handle_queue_add({"worker": "gelbooru", "tag": "b", "limit": "50"})
    Rems_Dl.handle_start_worker({"worker": "gelbooru"})
    assert _wait(lambda: len(dispatches) == 2 and _drained()), "run never settled"
    assert [got for _, got in dispatches] == [50, 50]


def _drained():
    return not Rems_Dl.ACTIVE_JOBS


def test_stop_clears_run_budget(budget_env):
    Rems_Dl.handle_start_worker({"worker": "gelbooru", "tag": "a", "limit": "2"})
    assert Rems_Dl.RUN_BUDGETS.get("gelbooru") == 2
    Rems_Dl.handle_stop_worker({"worker": "gelbooru"})
    assert "gelbooru" not in Rems_Dl.RUN_BUDGETS


def test_new_start_resets_spent_budget(budget_env):
    # spend a run dry, then a fresh START payload raises the budget again
    dispatches = budget_env
    Rems_Dl.handle_queue_add({"worker": "gelbooru", "tag": "b", "limit": "5"})
    Rems_Dl.handle_queue_add({"worker": "gelbooru", "tag": "c", "limit": "5"})
    Rems_Dl.handle_start_worker({"worker": "gelbooru", "tag": "a", "limit": "2"})
    assert _wait(lambda: len(dispatches) == 1 and _drained()), "run 1 never settled"
    Rems_Dl.handle_start_worker({"worker": "gelbooru", "tag": "x", "limit": "50"})
    assert _wait(lambda: len(dispatches) == 4 and _drained()), "run 2 never settled"
    # fresh budget 50: c(5) + a(2) spend 7, x wants 50 — clamped to the
    # remaining 43 — the whole run still totals exactly 50
    assert [got for _, got in dispatches[1:]] == [5, 2, 43]
    assert not Rems_Dl.DOWNLOAD_QUEUES["gelbooru"]
