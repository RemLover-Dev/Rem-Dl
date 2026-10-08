"""Gallery rating filter: per-image rating wins, legacy paths match segments.

nekosapi puts a multi-rating run into ONE Safe_Sensitive_Questionable folder.
The old substring match let that name pass Safe, Sensitive, AND Questionable
filters, and the client sniffed every file in it as questionable — so the
Safe filter showed blurred NSFW images. These pin the three fixes: stored
per-image rating answers first, legacy path matching is segment-exact, and
single-label folders / tag-based ratings keep working.
"""
import os

# Rems_Dl's load_dotenv() pollutes os.environ, which SettingsManager reads for
# its defaults — snapshot and restore (same dance as test_gallery_exclude_search,
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


def _names(images, ratings):
    out = Rems_Dl._apply_gallery_filters(list(images), "", [], False, [], list(ratings))
    return [i["filename"] for i in out]


MIXED = {"site": "nekosapi", "filename": "253.webp",
         "filepath": "NekosAPI/catgirl/Safe_Sensitive_Questionable/253.webp",
         "tags": {"general": ["catgirl"]}}
SAFEDIR = {"site": "nekosapi", "filename": "1.webp",
           "filepath": "NekosAPI/catgirl/Safe/1.webp",
           "tags": {"general": ["catgirl"]}}
NSFWDIR = {"site": "nekosapi", "filename": "9.webp",
           "filepath": "NekosAPI/catgirl/NSFW/9.webp",
           "tags": {"general": ["catgirl"]}}


def test_mixed_folder_passes_no_rating_filter():
    # no rating selected = no rating filtering at all
    assert _names([MIXED, SAFEDIR, NSFWDIR], []) == ["253.webp", "1.webp", "9.webp"]


def test_mixed_folder_fails_safe_filter():
    # the bug: "Safe_Sensitive_Questionable" contained "safe" as a substring
    assert _names([MIXED], ["safe"]) == []


def test_mixed_folder_fails_sensitive_and_questionable():
    assert _names([MIXED], ["sensitive"]) == []
    assert _names([MIXED], ["questionable"]) == []


def test_single_label_folders_still_match():
    assert _names([SAFEDIR], ["safe"]) == ["1.webp"]
    assert _names([SAFEDIR], ["explicit"]) == []
    assert _names([NSFWDIR], ["explicit"]) == ["9.webp"]
    assert _names([NSFWDIR], ["safe"]) == []


def test_stored_rating_beats_mixed_folder_name():
    stored_safe = dict(MIXED, rating="safe")
    assert _names([stored_safe], ["safe"]) == ["253.webp"]
    assert _names([stored_safe], ["questionable"]) == []
    stored_q = dict(MIXED, rating="questionable")
    assert _names([stored_q], ["safe"]) == []
    assert _names([stored_q], ["questionable"]) == ["253.webp"]


def test_stored_rating_beats_single_label_folder():
    # download-time truth wins even if the record sits under a wrong folder
    stored_exp = dict(SAFEDIR, rating="explicit")
    assert _names([stored_exp], ["safe"]) == []
    assert _names([stored_exp], ["explicit"]) == ["1.webp"]


def test_junk_stored_rating_falls_back_to_sniffing():
    junk = dict(SAFEDIR, rating="NSFW-ish")
    assert _names([junk], ["safe"]) == ["1.webp"]


def test_tag_based_rating_still_works():
    gel = {"site": "gelbooru", "filename": "b.png",
           "filepath": "gelbooru/kafka/b.png", "tags": ["rating:q", "kafka"]}
    assert _names([gel], ["questionable"]) == ["b.png"]
    assert _names([gel], ["safe"]) == []
    gen = {"site": "gelbooru", "filename": "a.png",
           "filepath": "gelbooru/safe_tag/a.png", "tags": ["rating:g", "kafka"]}
    assert _names([gen], ["safe"]) == ["a.png"]


def test_rule34_all_explicit_special_case():
    r34 = {"site": "rule34", "filename": "x.jpg",
           "filepath": "rule34/x.jpg", "tags": ["whatever"]}
    assert _names([r34], ["explicit"]) == ["x.jpg"]
    assert _names([r34], ["safe"]) == []


def test_inherently_safe_site_passes_unless_marked():
    z = {"site": "zerochan", "filename": "z.png",
         "filepath": "zerochan/z.png", "tags": ["kafka"]}
    assert _names([z], ["safe"]) == ["z.png"]
    bad = {"site": "zerochan", "filename": "n.png",
           "filepath": "zerochan/NSFW/n.png", "tags": ["kafka"]}
    assert _names([bad], ["safe"]) == []
