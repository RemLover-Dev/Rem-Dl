import os
import types

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


def test_cleanup_removes_rows_caches_and_emits(monkeypatch):
    calls = {}
    gallery = {"images": [
        {"filename": "a.jpg", "filepath": "sub/a.jpg"},
        {"filename": "gone.jpg", "filepath": "sub/gone.jpg"},
    ]}
    deleted_rows, history_rows, events = [], [], []
    class FakeStore:
        def delete_by_filename(self, fn): deleted_rows.append(fn)
    monkeypatch.setattr(shared, "load_gallery", lambda: gallery)
    monkeypatch.setattr(shared, "_store", lambda: FakeStore())
    monkeypatch.setattr(Rems_Dl, "_invalidate_fp_cache", lambda: calls.__setitem__("fp", True))
    monkeypatch.setattr(Rems_Dl, "_evict_caches", lambda p: calls.__setitem__("evict", p))
    monkeypatch.setattr(Rems_Dl.DatabaseManager, "remove_image_history",
                        lambda fn: history_rows.append(fn))
    monkeypatch.setattr(Rems_Dl, "socketio",
                        types.SimpleNamespace(emit=lambda e, d: events.append((e, d))))

    Rems_Dl._dedup_replaced_cleanup(os.path.join("roots", "sub", "gone.jpg"))

    assert deleted_rows == ["gone.jpg"]
    assert history_rows == ["gone.jpg"]
    assert events == [("image_deleted", {"filename": "gone.jpg"})]
    assert [i["filename"] for i in gallery["images"]] == ["a.jpg"]
    assert calls == {"fp": True, "evict": os.path.join("roots", "sub", "gone.jpg")}


def test_cleanup_without_gallery_row_does_not_emit(monkeypatch):
    gallery = {"images": [{"filename": "a.jpg", "filepath": "sub/a.jpg"}]}
    deleted_rows, events = [], []

    class FakeStore:
        def delete_by_filename(self, fn): deleted_rows.append(fn)
    monkeypatch.setattr(shared, "load_gallery", lambda: gallery)
    monkeypatch.setattr(shared, "_store", lambda: FakeStore())
    monkeypatch.setattr(Rems_Dl, "_invalidate_fp_cache", lambda: None)
    monkeypatch.setattr(Rems_Dl, "_evict_caches", lambda p: None)
    monkeypatch.setattr(Rems_Dl.DatabaseManager, "remove_image_history", lambda fn: None)
    monkeypatch.setattr(Rems_Dl, "socketio",
                        types.SimpleNamespace(emit=lambda e, d: events.append((e, d))))

    Rems_Dl._dedup_replaced_cleanup(os.path.join("roots", "missing.jpg"))

    assert deleted_rows == []
    # history row is always attempted (the file is gone either way), but an
    # event with no gallery row gone never happens
    assert events == []


def test_cleanup_replaced_swallows_hook_errors(monkeypatch, capsys):
    def boom(_p):
        raise RuntimeError("hook exploded")

    monkeypatch.setattr(shared, "dedup_replaced_hook", boom)
    shared.cleanup_replaced("/x/y.jpg")  # must not raise
    assert "Dedup replace cleanup error" in capsys.readouterr().out

    monkeypatch.setattr(shared, "dedup_replaced_hook", None)
    shared.cleanup_replaced("/x/y.jpg")  # no hook = no-op
