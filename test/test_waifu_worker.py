import pytest

import core.shared as shared
from workers.waifu_im import WaifuImWorker, waifu_name_to_slug


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


def test_waifu_include_and_exclude_slugs(master):
    w = WaifuImWorker("waifu maid -uniform -lingerie", 10, "", [], NET_CONFIG)
    assert w.include_slugs == ["waifu", "maid"]
    assert w.exclude_slugs == ["uniform", "lingerie"]
    assert w.safe_tag == "waifu maid"


def test_waifu_exclusions_arg_merges_with_dash_tokens(master):
    w = WaifuImWorker("waifu -uniform", 10, "", ["-lingerie", "glasses"], NET_CONFIG)
    assert w.include_slugs == ["waifu"]
    assert w.exclude_slugs == ["uniform", "lingerie", "glasses"]


def test_waifu_exclusion_only_query_browses_all(master):
    # tag is only "-uniform": no IncludedTags -> browse everything minus excluded
    w = WaifuImWorker("-uniform", 10, "", [], NET_CONFIG)
    assert w.include_slugs == []
    assert w.exclude_slugs == ["uniform"]
    assert w.safe_tag == "waifu"


def test_waifu_empty_tag_falls_back_to_all(master):
    w = WaifuImWorker("", 10, "", [], NET_CONFIG)
    assert w.include_slugs == []
    assert w.exclude_slugs == []
    assert w.safe_tag == "waifu"


def test_waifu_rating_param_mapping(master):
    # docs: False = SFW only, True = NSFW only, All = both
    assert WaifuImWorker("", 10, "", [], NET_CONFIG).is_nsfw_param() == "All"
    assert WaifuImWorker("", 10, "safe", [], NET_CONFIG).is_nsfw_param() == "False"
    assert WaifuImWorker("", 10, "nsfw", [], NET_CONFIG).is_nsfw_param() == "True"
    assert WaifuImWorker("", 10, "safe nsfw", [], NET_CONFIG).is_nsfw_param() == "All"


def test_waifu_page_params_multi_tag(master):
    w = WaifuImWorker("waifu maid -uniform", 10, "nsfw", [], NET_CONFIG)
    p = w.page_params(3)
    assert p["pageSize"] == 50
    assert p["page"] == 3
    assert p["IsNsfw"] == "True"
    assert p["IncludedTags"] == ["waifu", "maid"]
    assert p["ExcludedTags"] == ["uniform"]


def test_waifu_page_params_omits_empty_lists(master):
    w = WaifuImWorker("-uniform", 10, "", [], NET_CONFIG)
    p = w.page_params(1)
    assert "IncludedTags" not in p
    assert p["ExcludedTags"] == ["uniform"]
    assert p["IsNsfw"] == "All"


def test_waifu_slug_fallback_and_name_map(master, monkeypatch):
    # unmapped names slugify by lowering + hyphenating spaces
    assert waifu_name_to_slug("Long Hair") == "long-hair"
    monkeypatch.setitem(shared.WAIFU_TAG_MAP, "long hair", "long_hair")
    assert waifu_name_to_slug("Long Hair") == "long_hair"
