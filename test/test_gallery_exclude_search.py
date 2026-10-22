"""Gallery tab search: comma-separated tags, '-term' excludes.

Commas separate tags so multi-word tags need no underscores (underscore ==
space still works on both sides). Exclusion is substring like positives.
Intake lowercases the query (get_gallery / get_gallery_sources).

The search is answered by the database (shared.search_tag_ids) instead of
materialising every tag row, so these run against a seeded store: they pin
that the SQL and the matcher they replaced still agree on every documented
case.
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

import pytest

import Rems_Dl
from core.gallery_store import get_store, use_store_path

for _k, _v in _env_before.items():
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v

IMAGES = [
    {"id": "a", "site": "gelbooru", "filename": "a.png",
     "tags": ["hoshimachi_suisei", "tokoyami_towa"]},
    {"id": "b", "site": "gelbooru", "filename": "b.png",
     "tags": ["kafka_(honkai:_star_rail)", "honkai:_star_rail"]},
    {"id": "c", "site": "gelbooru", "filename": "c.png",
     "tags": ["fire_keeper", "ai_generated"]},
    # mixed-case on purpose: intake stores tags as they came, the filter
    # normalises both sides
    {"id": "d", "site": "zerochan", "filename": "d.png",
     "tags": ["Kafka (Arknights)"]},
]

BRA = [
    {"id": "p1", "site": "gelbooru", "filename": "both.png",
     "tags": ["bra", "braid"]},
    {"id": "p2", "site": "gelbooru", "filename": "only_braid.png",
     "tags": ["braid"]},
    {"id": "p3", "site": "gelbooru", "filename": "only_bra.png",
     "tags": ["long_bra"]},
]


@pytest.fixture
def seeded(tmp_path):
    """A store holding IMAGES + BRA, and nothing else."""
    use_store_path(str(tmp_path / "gallery.db"))
    store = get_store()
    for e in IMAGES + BRA:
        store.insert_entry({
            "id": e["id"], "filename": e["filename"],
            "filepath": "%s/%s" % (e["site"], e["filename"]),
            "site": e["site"], "favourite": False,
            "downloaded_at": "2026-01-01T00:00:00", "tags": e["tags"],
        })
    yield
    use_store_path(None)


def _search(q, images, seeded):
    """Exactly what _filter_gallery does for a search, minus the timestamp range."""
    keep = Rems_Dl._search_id_set(q.lower().strip())
    out = Rems_Dl._apply_gallery_filters(list(images), keep, [], False, [], [])
    return [i["filename"] for i in out]


def test_bare_term_stays_substring(seeded):
    assert _search("bra", BRA, seeded) == ["both.png", "only_braid.png", "only_bra.png"]


def test_double_quoted_exact_match(seeded):
    assert _search('"bra"', BRA, seeded) == ["both.png"]
    assert _search('"braid"', BRA, seeded) == ["both.png", "only_braid.png"]


def test_single_quoted_exact_match(seeded):
    assert _search("'bra'", BRA, seeded) == ["both.png"]
    assert _search("'braid'", BRA, seeded) == ["both.png", "only_braid.png"]


def test_quoted_exact_not_prefix(seeded):
    # exact means whole tag: long_bra has "bra" inside but isn't "bra"
    assert _search('"long bra"', BRA, seeded) == ["only_bra.png"]


def test_quoted_exclusion(seeded):
    # only the exact tag "bra" is excluded — long_bra and braid survive
    assert _search('-"bra"', BRA, seeded) == ["only_braid.png", "only_bra.png"]
    assert _search("'bra', braid", BRA, seeded) == ["both.png"]


def test_quoted_mixed_with_plain(seeded):
    assert _search('"bra", braid', BRA, seeded) == ["both.png"]


def test_single_tag(seeded):
    assert _search("kafka", IMAGES, seeded) == ["b.png", "d.png"]
    assert _search("fire", IMAGES, seeded) == ["c.png"]


def test_comma_separates_tags(seeded):
    assert _search("kafka, honkai", IMAGES, seeded) == ["b.png"]
    assert _search("kafka ,honkai", IMAGES, seeded) == ["b.png"]  # spaces around commas are fine


def test_multiword_tag_without_underscores(seeded):
    # the whole point: spaces inside a tag, no underscores forced
    assert _search("kafka (arknights)", IMAGES, seeded) == ["d.png"]
    assert _search("hoshimachi suisei", IMAGES, seeded) == ["a.png"]


def test_underscore_form_still_works(seeded):
    assert _search("honkai:_star_rail", IMAGES, seeded) == ["b.png"]
    assert _search("hoshimachi_suisei", IMAGES, seeded) == ["a.png"]


def test_exclude_tag(seeded):
    assert _search("-kafka", IMAGES, seeded) == ["a.png", "c.png"]
    assert _search("kafka, -honkai", IMAGES, seeded) == ["d.png"]
    assert _search("-ai_generated", IMAGES, seeded) == ["a.png", "b.png", "d.png"]


def test_exclude_multiword_tag(seeded):
    assert _search("-kafka (arknights)", IMAGES, seeded) == ["a.png", "b.png", "c.png"]


def test_lone_dash_stays_positive(seeded):
    # bare "-" has no term to exclude — needs a tag containing literal "-"
    assert _search("kafka, -", IMAGES, seeded) == []


def test_like_metacharacters_are_literal(seeded):
    # a '%' typed into the box must not match everything
    assert _search("%", IMAGES, seeded) == []
    assert _search("ai_gen%", IMAGES, seeded) == []


def test_exact_match_is_case_insensitive(seeded):
    # stored tags are lowered at index time; an uppercase exact term used to
    # be compared raw and could never match
    store = get_store()
    store.insert_entry({
        "id": "e", "filename": "e.png", "filepath": "gelbooru/e.png",
        "site": "gelbooru", "favourite": False,
        "downloaded_at": "2026-01-01T00:00:00", "tags": ["ALIENSTAGE"],
    })
    images = IMAGES + BRA + [{"id": "e", "filename": "e.png",
                              "filepath": "gelbooru/e.png", "site": "gelbooru"}]
    assert _search('"alienstage"', images, seeded) == ["e.png"]


def test_index_survives_a_tag_write(seeded):
    # the search index is derived from gallery_tags; a write after the first
    # search must still be visible to the next one
    store = get_store()
    store.insert_entry({
        "id": "late", "filename": "late.png", "filepath": "gelbooru/late.png",
        "site": "gelbooru", "favourite": False,
        "downloaded_at": "2026-01-01T00:00:00", "tags": ["hoshimachi_suisei"],
    })
    images = IMAGES + [{"id": "late", "filename": "late.png",
                        "filepath": "gelbooru/late.png", "site": "gelbooru"}]
    assert _search("suisei", images, seeded) == ["a.png", "late.png"]


def test_tag_write_maintains_a_built_index(seeded):
    # the perf contract behind concurrent use: a tag write (per downloaded
    # image, per healed entry) must keep the built index, not drop it —
    # dropping made every search during a download session pay the full
    # ~770ms rebuild, which is the "gallery search crawls while the app is
    # busy" regression. Results must also reflect the write in both
    # directions: added tags findable, removed tags gone.
    store = get_store()
    _search("kafka", IMAGES, seeded)              # forces the build
    assert store._norm_idx is not None
    store.set_tags("d", ["totally_new_tag"])      # the hot write path
    assert store._norm_idx is not None            # maintained, not dropped
    assert _search("kafka", IMAGES, seeded) == ["b.png"]
    assert _search("totally_new_tag", IMAGES, seeded) == ["d.png"]


def test_exact_positive_with_exclusion_leaves_the_index_intact(seeded):
    # _search_id_set mutates its first result in place (`keep -=`); handing
    # it the live index set for an exact term let this search carve p1 out of
    # the index itself, so the next exact "bra" search came back empty
    assert _search('"bra", -braid', BRA, seeded) == []
    assert _search('"bra"', BRA, seeded) == ["both.png"]


def test_delete_drops_ids_from_the_index(seeded):
    store = get_store()
    _search("kafka", IMAGES, seeded)              # forces the build
    store.delete("d")
    assert _search("kafka", IMAGES, seeded) == ["b.png"]


def test_warm_index_builds_before_the_first_search(seeded):
    # startup warms the index in a background thread so the first user search
    # doesn't pay the one-off build; it must be idempotent and answer
    # correctly afterwards
    store = get_store()
    assert store._norm_idx is None
    store.warm_search_index()
    assert store._norm_idx is not None
    built = store._norm_idx
    store.warm_search_index()                    # no-op the second time
    assert store._norm_idx is built
    assert _search("kafka", IMAGES, seeded) == ["b.png", "d.png"]


def test_all_negation_keeps_everything_else(seeded):
    # no positive term: the set starts from the whole library, not from nothing
    assert _search("-nothing_matches_this", IMAGES, seeded) == [
        "a.png", "b.png", "c.png", "d.png"]
