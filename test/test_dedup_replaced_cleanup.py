"""Tests for keep-larger dedup replacement cleanup hand-off."""
from core import shared


def test_cleanup_replaced_calls_two_argument_hook_with_stat(monkeypatch):
    calls = []
    monkeypatch.setattr(shared, "dedup_replaced_hook",
                        lambda path, stat: calls.append((path, stat)))
    shared.cleanup_replaced("/old/image.jpg", (123, 456))
    assert calls == [("/old/image.jpg", (123, 456))]


def test_cleanup_replaced_supports_legacy_one_argument_hook(monkeypatch):
    calls = []
    monkeypatch.setattr(shared, "dedup_replaced_hook", lambda path: calls.append(path))
    shared.cleanup_replaced("/old/image.jpg", (123, 456))
    assert calls == ["/old/image.jpg"]


def test_cleanup_replaced_swallows_hook_errors(monkeypatch, capsys):
    def fail(_path, _stat=None):
        raise RuntimeError("broken cleanup")
    monkeypatch.setattr(shared, "dedup_replaced_hook", fail)
    assert shared.cleanup_replaced("/old/image.jpg", (123, 456)) is None
    assert "Dedup replace cleanup error" in capsys.readouterr().out


def test_gallery_path_identity_keeps_same_basename_separate(tmp_path, monkeypatch):
    root = tmp_path / "library"
    a = root / "site-a" / "image.jpg"
    b = root / "site-b" / "image.jpg"
    monkeypatch.setattr(shared, "MASTER_FOLDER", str(root))

    assert shared.gallery_path_key(str(a)) != shared.gallery_path_key(str(b))
    assert shared.gallery_id_for_path("site-a", str(a), "image.jpg") != \
           shared.gallery_id_for_path("site-b", str(b), "image.jpg")


def test_gallery_path_identity_normalizes_relative_and_absolute_paths(tmp_path, monkeypatch):
    root = tmp_path / "library"
    target = root / "site" / "image.jpg"
    monkeypatch.setattr(shared, "MASTER_FOLDER", str(root))
    assert shared.gallery_path_key("site/image.jpg") == shared.gallery_path_key(str(target))
