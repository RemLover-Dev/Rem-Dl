"""The scheduler pool must not double-run a watcher or starve others."""
from core import watchers


def test_idle_due_skips_inflight(monkeypatch):
    rows = [{"watcher_id": "a", "source": "gelbooru", "enabled": True,
             "last_checked_at": 0, "interval_minutes": 5},
            {"watcher_id": "b", "source": "danbooru", "enabled": True,
             "last_checked_at": 0, "interval_minutes": 5}]
    monkeypatch.setattr(watchers, "list_watchers", lambda *a, **k: rows)
    with watchers._INFLIGHT_LOCK:
        watchers._INFLIGHT.add("a")
    try:
        assert [w["watcher_id"] for w in watchers._idle_due(now=10**12)] == ["b"]
    finally:
        with watchers._INFLIGHT_LOCK:
            watchers._INFLIGHT.discard("a")


def test_claim_is_per_watcher():
    assert watchers._claim("a")
    try:
        assert not watchers._claim("a")
        assert watchers._claim("b")
        watchers._release("b")
    finally:
        watchers._release("a")
    assert watchers._claim("a")
    watchers._release("a")


def test_pool_size_follows_the_user_request_limit(monkeypatch):
    class S:
        def __init__(self, v):
            self.v = v

        def get(self, key, default=None):
            return self.v

    monkeypatch.setattr(watchers, "get_settings", lambda *a, **k: S("7"))
    assert watchers._pool_size() == 7
    monkeypatch.setattr(watchers, "get_settings", lambda *a, **k: S("junk"))
    assert watchers._pool_size() == 4
