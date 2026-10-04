"""Gallery tab search: comma-separated tags, '-term' excludes.

Commas separate tags so multi-word tags need no underscores (underscore ==
space still works on both sides). Exclusion is substring like positives.
Intake lowercases the query (get_gallery / get_gallery_sources).
"""
import os

# Rems_Dl's load_dotenv() pollutes os.environ, which SettingsManager reads for
# its defaults — snapshot and restore (same dance as test_gallery_no_tag_loss,
# needed because this file imports Rems_Dl first).
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

IMAGES = [
    {"site": "gelbooru", "filename": "a.png", "tags": ["hoshimachi_suisei", "tokoyami_towa"]},
    {"site": "gelbooru", "filename": "b.png", "tags": ["kafka_(honkai:_star_rail)", "honkai:_star_rail"]},
    {"site": "gelbooru", "filename": "c.png", "tags": ["fire_keeper", "ai_generated"]},
    {"site": "zerochan", "filename": "d.png", "tags": ["Kafka (Arknights)"]},
]


def _search(q):
    out = Rems_Dl._apply_gallery_filters(list(IMAGES), q.lower().strip(), [], False, [], [])
    return [i["filename"] for i in out]


def test_single_tag():
    assert _search("kafka") == ["b.png", "d.png"]
    assert _search("fire") == ["c.png"]


def test_comma_separates_tags():
    assert _search("kafka, honkai") == ["b.png"]
    assert _search("kafka ,honkai") == ["b.png"]  # spaces around commas are fine


def test_multiword_tag_without_underscores():
    # the whole point: spaces inside a tag, no underscores forced
    assert _search("kafka (arknights)") == ["d.png"]
    assert _search("hoshimachi suisei") == ["a.png"]


def test_underscore_form_still_works():
    assert _search("honkai:_star_rail") == ["b.png"]
    assert _search("hoshimachi_suisei") == ["a.png"]


def test_exclude_tag():
    assert _search("-kafka") == ["a.png", "c.png"]
    assert _search("kafka, -honkai") == ["d.png"]
    assert _search("-ai_generated") == ["a.png", "b.png", "d.png"]


def test_exclude_multiword_tag():
    assert _search("-kafka (arknights)") == ["a.png", "b.png", "c.png"]


def test_lone_dash_stays_positive():
    # bare "-" has no term to exclude — needs a tag containing literal "-"
    assert _search("kafka, -") == []
