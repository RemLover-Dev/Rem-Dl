import json

from core.planning_import import (
    DEFAULT_SITE,
    normalize_bulk_text,
    resolve_site,
)


# --- resolve_site ---
def test_exact_aliases():
    for term, site in [("dan", "dan"), ("danbooru", "dan"), ("gel", "gelbooru"),
                       ("yande.re", "yande"), ("kona", "kona"), ("konachan", "kona"),
                       ("pix", "pixiv"), ("anime_dl", "anime_dl"),
                       ("anime-pictures", "anime_dl"), ("e-shuushuu", "eshuushuu"),
                       ("r34", "rule34"), ("safebooru", "safe"), ("x", "x"),
                       ("pinterest", "pinterest")]:
        assert resolve_site(term) == (site, None)


def test_prefix_matching():
    assert resolve_site("yande") == ("yande", None)
    assert resolve_site("sanka") == ("sankaku", None)
    assert resolve_site("Konachan") == ("kona", None)


def test_ambiguous_and_unknown():
    site, cand = resolve_site("safe")
    assert site == "safe" and cand is None
    assert resolve_site("notasite") == (None, None)
    assert resolve_site("") == (None, None)


# --- grouping labels ---
def test_grouped_tags_with_repeated_labels():
    rows = normalize_bulk_text("""[21:34, 08/12/2025] Hans: taya_oco
Dan:
teffish
>> 42849078
""")
    assert [(r["site"], r["tags"]) for r in rows] == [
        ("dan", "taya_oco"), ("dan", "teffish"), ("dan", "42849078"),
    ]
    # the chat-noise number must not slip through as a silent tag
    assert any("bare number" in w for w in rows[2]["warnings"])


def test_inline_pairs():
    rows = normalize_bulk_text("""Dan: rem blue_hair
marisa_(touhou) gelbooru
rem gel
""")
    assert [(r["site"], r["tags"]) for r in rows] == [
        ("dan", "rem blue_hair"),
        ("gelbooru", "marisa_(touhou)"),
        ("gelbooru", "rem"),
    ]


def test_pure_number_pixiv_lines():
    rows = normalize_bulk_text("Pixiv: Pixiv\n42849078\n")
    assert (rows[0]["site"], rows[0]["tags"]) == ("pixiv", "artworks:42849078")
    # number without pixiv context is kept (warned) — never silently dropped
    rows = normalize_bulk_text("91659414\n")
    assert "bare number '91659414' is only auto-detected" in " ".join(rows[0]["warnings"])
    assert "91659414" in rows[0]["tags"]


def test_unlabeled_lines_default_gelbooru():
    rows = normalize_bulk_text("some cool art")
    assert len(rows) == 1
    assert rows[0]["site"] == DEFAULT_SITE
    assert "defaulted to gelbooru" in rows[0]["warnings"][0]


def test_siteless_pending_merge():
    rows = normalize_bulk_text("rem\nblack_hair\nkona\n")
    assert len(rows) == 1
    assert (rows[0]["site"], rows[0]["tags"]) == ("kona", "rem black_hair")


def test_trailing_pending_defaults():
    rows = normalize_bulk_text("lol kitten\n")
    assert rows[0]["site"] == DEFAULT_SITE


# --- exclusions / ratings / noise ---
def test_exclusion_tokens_preserved():
    rows = normalize_bulk_text("gel: rem blue_hair -scosci")
    assert rows[0]["tags"] == "rem blue_hair -scosci"
    assert rows[0]["site"] == "gelbooru"


def test_exclusion_line_attaches_to_previous_entry():
    rows = normalize_bulk_text("gel: rem\n-ai_generated")
    assert [(r["site"], r["tags"]) for r in rows] == [("gelbooru", "rem -ai_generated")]
    assert any("attached to the previous entry" in w for w in rows[0]["warnings"])
    # an exclusion under a different site stays its own entry
    rows = normalize_bulk_text("gel: rem\ndan: marisa\n-Y tag")
    assert [r["site"] for r in rows] == ["gelbooru", "dan", "dan"]


def test_stray_dash_exclusion_marker():
    rows = normalize_bulk_text("kona\nrem - video")
    assert rows[0]["tags"] == "rem -video"
    assert "exclusion marker" in rows[0]["warnings"][0]


def test_multi_rating_tokens():
    rows = normalize_bulk_text("dan rem rating:s rating:q")
    assert rows[0]["tags"] == "rem"
    assert rows[0]["rating"] == "rating:s"  # first wins
    assert not any("rating token" in w for w in rows[0]["warnings"])


def test_rating_only_lines_not_lost():
    rows = normalize_bulk_text("kona\nrating:s\nrem\n")
    dropped = [r for r in rows if "rating token dropped" in " ".join(r.get("warnings", [""]))]
    assert dropped, "the stray rating token must show up as a row"


def test_urls_note():
    import re
    rows = normalize_bulk_text("see https://gelbooru.com loli_no")
    assert len(rows) == 1
    note = rows[0]["note"]
    assert re.match(r"https://gelbooru\.com/?", note)
    assert "loli_no" in rows[0]["tags"]
    assert "https://gelbooru.com" not in rows[0]["tags"]


def test_symbol_and_jabber_lines():
    rows = normalize_bulk_text(">>>\n| WXx |")
    assert rows == [] or all(r["tags"] != ">>" for r in rows)


def test_unknown_lines_flagged():
    rows = normalize_bulk_text("from unknown source")
    assert len(rows) == 1
    assert rows[0]["tags"] == "from unknown source"
    assert rows[0]["warnings"]


# --- duplicates ---
def test_duplicate_and_existing_flags():
    existing = [{"site": "dan", "tags": "taya_oco", "rating": ""}]
    rows = normalize_bulk_text("""dan
taya_oco
taya_oco
kona
taya_oco
""", existing=existing)
    sites = [r["site"] for r in rows]
    tags = [r["tags"] for r in rows]
    assert sites == ["dan", "dan", "kona"]
    assert tags == ["taya_oco", "taya_oco", "taya_oco"]
    assert rows[0]["exists"] is True
    assert rows[1]["duplicate"] is True   # repeated within the paste
    assert rows[2]["duplicate"] is False  # different site — same string OK


# --- timestamps / kickers / bullets ---
def test_timestamps_kickers_bullets():
    # each line alone — site-less lines merge in the real flow anyway
    rows = normalize_bulk_text("[10:34] Hans: rem blue_hair")
    assert rows[0]["tags"] == "rem blue_hair"
    rows = normalize_bulk_text("2021-05-04 12:34:56UTC marisa_(touhou)")
    assert rows[0]["tags"] == "marisa_(touhou)"
    # bullet keeps '-' exclusion semantics, several tokens become several tags
    rows = normalize_bulk_text("- rem smash")
    assert rows[0]["tags"] == "-rem -smash"


def test_site_kicker_not_peeled():
    rows = normalize_bulk_text("Dan: rem blue_hair")
    assert (rows[0]["site"], rows[0]["tags"]) == ("dan", "rem blue_hair")


def test_label_rating_carried_to_following_lines():
    rows = normalize_bulk_text("Dan: rating:s\nrem\nkona: marisa\nrem yande")
    assert rows[0]["rating"] == "rating:s"
    # the label rating never leaks into other sites' entries
    assert all(r["rating"] == "" for r in rows[1:])
    assert rows[1]["site"] == "kona"


def test_bracket_datetime_lead_stripped():
    rows = normalize_bulk_text("[2026-01-01 10:34] rem blue_hair")
    assert rows[0]["tags"] == "rem blue_hair"


def test_telegram_clipboard_stamp_and_bare_sender():
    # date-first 12h stamp in brackets + sender line with no message
    assert normalize_bulk_text("[10/25/26 8:22 PM] Hanekawa: ") == []
    assert normalize_bulk_text("[10/25/26 8:22 PM] Hanekawa:") == []
    rows = normalize_bulk_text("[10/25/26 8:22 PM] Hanekawa: rem blue_hair")
    assert rows[0]["tags"] == "rem blue_hair"
    # a bare sender line between entries drops, site labels survive it
    rows = normalize_bulk_text("Dan: rating:e\n[10/25/26 8:22 PM] Hanekawa: \nrem")
    assert [(r["site"], r["tags"], r["rating"]) for r in rows] == [("dan", "rem", "rating:e")]


def test_case_insensitive_site_labels():
    rows = normalize_bulk_text("YANDE.RE\nrem\n")
    assert (rows[0]["site"], rows[0]["tags"]) == ("yande", "rem")


def test_bracket_wrapped_tags():
    rows = normalize_bulk_text("[gel]\n(rem danbooru)\n")
    # '[gel]' is a pure label line (no entry), the pair line closes as dan
    assert len(rows) == 1
    assert (rows[0]["site"], rows[0]["tags"]) == ("dan", "rem")


def test_json_cache_roundtrip(tmp_path, monkeypatch):
    """The resolved rows should serialize — the route just jsonifies them."""
    rows = normalize_bulk_text("kon\nrem (touhou) -red\n")
    out = json.dumps(rows)
    assert json.loads(out) == rows
