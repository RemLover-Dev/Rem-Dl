from core.shared import tags_dict_from_lists


def test_repeated_tags_collapse_to_first_seen():
    d = tags_dict_from_lists(["brown_hair", "girl", "brown_hair"],
                             ["alice", "bob", "alice"])
    assert d["tag"] == ["brown_hair", "girl"]
    assert d["artist"] == ["alice", "bob"]


def test_whitespace_variants_collapse():
    d = tags_dict_from_lists(["  cat ", "cat", "", "  "])
    assert d["tag"] == ["cat"]


def test_other_buckets_dedupe_too():
    d = tags_dict_from_lists(None, None, ["hatsune", "hatsune"],
                             ["touhou"], None, ["maid", "maid"])
    assert d["character"] == ["hatsune"]
    assert d["copyright"] == ["touhou"]
    assert d["outfit"] == ["maid"]
