"""Settings-tab hook + conversion pipeline for auto JXL after download."""
import json
import os
from pathlib import Path

import pytest




def test_hook_disabled_ignores_download(tmp_path, monkeypatch):
    import Rems_Dl
    Rems_Dl.settings.config["auto_convert_jxl"] = False
    seen = []
    monkeypatch.setattr(Rems_Dl, "_auto_jxl_queue", seen)
    Rems_Dl._auto_jxl_hook(str(tmp_path / "x.png"))
    assert seen == []


def test_hook_enqueues_clamped_effort(tmp_path, monkeypatch):
    import Rems_Dl
    seen = []

    class _Q:
        def put(self, item): seen.append(item)

    monkeypatch.setattr(Rems_Dl, "_auto_jxl_queue", _Q())
    monkeypatch.setattr(Rems_Dl.settings, "config", {"auto_convert_jxl": True, "jxl_effort": "77"})
    Rems_Dl._auto_jxl_hook(str(tmp_path / "x.png"))
    assert seen == [(str(tmp_path / "x.png"), 9)]


def test_hook_bad_effort_falls_back(tmp_path, monkeypatch):
    import Rems_Dl
    seen = []

    class _Q:
        def put(self, item): seen.append(item)

    monkeypatch.setattr(Rems_Dl, "_auto_jxl_queue", _Q())
    monkeypatch.setattr(Rems_Dl.settings, "config", {"auto_convert_jxl": True, "jxl_effort": None})
    Rems_Dl._auto_jxl_hook(str(tmp_path / "x.png"))
    assert seen == [(str(tmp_path / "x.png"), 5)]


def _tiny_png(path):
    from PIL import Image
    Image.new("RGB", (8, 8), (10, 20, 30)).save(path, format="PNG")
    return path


@pytest.fixture
def inprocess_worker(monkeypatch):
    """Run the real one-file worker inside the test process instead of a
    subprocess (same code path, faster failures)."""
    import Rems_Dl

    class _Proc:
        returncode = 0

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    def fake_spawn(request_path, response_path):
        from core.jxl_worker import run as worker_run
        worker_run(request_path, response_path)
        return _Proc()

    with monkeypatch.context() as patch:
        patch.setattr(Rems_Dl, "_auto_jxl_spawn_worker", fake_spawn)
        yield


def test_convert_preserves_tags_dates_and_swaps(tmp_path, monkeypatch, inprocess_worker):
    pytest.importorskip("pillow_jxl")
    import Rems_Dl
    from core import shared

    png = _tiny_png(str(tmp_path / "image1.png"))
    tags = ["1girl", "solo"]
    artists, characters, copyrights = ["re:m"], ["rem"], ["re:zero kara hajimeru isekai seikatsu"]
    shared.write_image_metadata(png, tags, artists, "zerochan", characters, copyrights)
    monkeypatch.setattr(shared, "MASTER_FOLDER", str(tmp_path))
    shared.add_to_gallery("zerochan", "image1.png", "image1.png", tags, artists, characters, copyrights)
    stat = os.stat(png)
    tags_before = shared.read_image_metadata(png)

    Rems_Dl._auto_jxl_convert_one(str(png), 5)

    target = Path(str(png)).with_suffix(".jxl")
    assert not Path(str(png)).exists()
    assert target.exists()
    assert os.stat(target).st_mtime_ns == stat.st_mtime_ns
    assert shared.read_image_metadata(target) == tags_before
