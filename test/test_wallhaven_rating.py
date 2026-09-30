import os

import pytest

from workers.wallhaven import WallhavenWorker, category_bits, purity_bits


NET_CONFIG = {
    "anti_ban_pause": 0.0,
    "download_retries": 1,
    "use_proxy": False,
    "api_timeout": 5,
}


@pytest.fixture
def master(tmp_path, monkeypatch):
    monkeypatch.setattr("core.shared.MASTER_FOLDER", str(tmp_path))
    return tmp_path


def test_purity_bits_mapping():
    assert purity_bits("") == "111"
    assert purity_bits("sfw") == "100"
    assert purity_bits("sfw sketchy") == "110"
    assert purity_bits("nsfw") == "001"
    assert purity_bits("sfw sketchy nsfw") == "111"
    assert purity_bits("banana") == "000"


def test_category_bits_mapping():
    assert category_bits("") == "111"
    assert category_bits("general") == "100"
    assert category_bits("general people") == "101"
    assert category_bits("anime") == "010"


def test_sorting_and_order_whitelist(master):
    w = WallhavenWorker("rem", 10, "sfw", "", "banana", "sideways", NET_CONFIG)
    assert w.sorting == "date_added"
    assert w.order == "desc"
    w2 = WallhavenWorker("rem", 10, "sfw", "", "toplist", "asc", NET_CONFIG)
    assert w2.sorting == "toplist"
    assert w2.order == "asc"


def test_tag_dir_created_and_purity_read(master):
    w = WallhavenWorker("rem blue_hair", 10, "sfw nsfw", "", "views", "desc", NET_CONFIG)
    assert w.purity_bits == "101"
    assert os.path.isdir(w.tag_dir)
    assert w.tag_dir.endswith("rem blue_hair") or "rem blue_hair" in w.tag_dir


def test_apikey_from_net_config(master):
    cfg = dict(NET_CONFIG, wallhaven_apikey="k123")
    w = WallhavenWorker("rem", 5, "nsfw", "", "date_added", "desc", cfg)
    assert w.apikey == "k123"
    w2 = WallhavenWorker("rem", 5, "sfw", "", "date_added", "desc", NET_CONFIG)
    assert w2.apikey == ""


def test_empty_tag_allowed_latest(master):
    # empty query = latest wallpapers (valid wallhaven use case)
    w = WallhavenWorker("", 5, "", "", "date_added", "desc", NET_CONFIG)
    assert w.original_tag == ""
    assert w.purity_bits == "111"
