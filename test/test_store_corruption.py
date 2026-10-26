"""Store resilience: trailing-junk salvage in load_json + the tmp-file
rename race in atomic_io — the two failure modes that ate watchers.json
("tags disappear over time": a corrupt read returns [], the next scheduled
save wipes the store)."""
import errno
import json
import os

from core import database, watchers
from core.atomic_io import write_json, write_lines


def test_load_json_salvages_trailing_junk(tmp_path):
    f = str(tmp_path / "store.json")
    with open(f, "w", encoding="utf-8") as fh:
        fh.write('{"keep": "me"}  }')
    data = database.DatabaseManager.load_json(f)
    assert data == {"keep": "me"}
    # self-healed: a plain re-read parses clean now
    with open(f, encoding="utf-8") as fh:
        assert json.load(fh) == {"keep": "me"}
    # corrupt bytes kept for forensics
    with open(f + ".corrupt", encoding="utf-8") as fh:
        assert fh.read().strip() == '{"keep": "me"}  }'


def test_load_json_unparseable_returns_empty(tmp_path):
    f = str(tmp_path / "bad.json")
    with open(f, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    assert database.DatabaseManager.load_json(f) == []
    assert os.path.exists(f + ".corrupt")  # bytes preserved


def test_save_json_keeps_last_good_bak(tmp_path):
    f = str(tmp_path / "s.json")
    write_json(f, {"v": 1})
    with open(f + ".bak", encoding="utf-8") as fh:
        assert json.load(fh) == {"v": 1}
    write_json(f, {"v": 2})
    with open(f + ".bak", encoding="utf-8") as fh:
        assert json.load(fh) == {"v": 2}


def test_env_drops_no_bak(tmp_path):
    f = str(tmp_path / ".env")
    write_lines(f, ["USE_PROXY='false'\n"])
    assert not os.path.exists(f + ".bak")


def test_truncated_main_restored_from_bak(tmp_path):
    f = str(tmp_path / "w.json")
    good = {"watchers": [[{"tags": ["ringeko-chan"]}], []]}
    write_json(f, good)
    with open(f, "w", encoding="utf-8") as fh:
        fh.write('{"watchers": [')  # wrecked mid-write by tool or power loss
    assert database.DatabaseManager.load_json(f) == good
    with open(f, encoding="utf-8") as fh:
        assert json.load(fh) == good  # main file healed from the bak


def test_missing_main_is_fresh_state_even_with_bak(tmp_path):
    f = str(tmp_path / "w.json")
    write_json(f, {"old": True})
    os.unlink(f)  # deliberate delete beats the backup
    assert database.DatabaseManager.load_json(f) == []


def test_atomic_write_survives_tmp_deletion_race(tmp_path, monkeypatch):
    target = str(tmp_path / "w.json")
    calls = {"n": 0}
    real_replace = os.replace

    def cleaner_eats_tmp(src, dst):
        calls["n"] += 1
        if calls["n"] == 1:
            os.unlink(src)  # the cleaner deletes the fresh tmp file
            raise FileNotFoundError(errno.ENOENT, "tmp vanished mid-replace")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", cleaner_eats_tmp)
    write_json(target, {"x": 1})
    with open(target, encoding="utf-8") as fh:
        assert json.load(fh) == {"x": 1}
    assert calls["n"] == 2
    assert not [n for n in os.listdir(tmp_path) if n.endswith(".tmp")]


def test_watchers_survive_corrupt_store(tmp_path, monkeypatch):
    f = str(tmp_path / "watchers.json")
    payload = {"watchers": [{
        "watcher_id": "w_x", "source": "gelbooru", "tags": ["ringeko-chan"],
        "ratings": [], "interval_minutes": 5, "enabled": True,
        "initialized": True, "baseline_at": 1.0,
        "last_seen_post_id": None, "last_checked_at": 0,
        "scan_pid": 0, "scan_newest_id": None, "error": ""}]}
    with open(f, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(payload) + "}}")
    monkeypatch.setattr(watchers, "WATCHERS_FILE", f)

    ws = watchers.list_watchers()
    assert [w["tags"] for w in ws] == [["ringeko-chan"]]

    out, err = watchers.update("w_x", {"tags": ["mika_pikazo"]})
    assert err is None and out["tags"] == ["mika_pikazo"]
    with open(f, encoding="utf-8") as fh:
        healed = json.load(fh)
    assert healed["watchers"][0]["tags"] == ["mika_pikazo"]
