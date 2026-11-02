import pytest

from core.database import DatabaseManager


@pytest.fixture(autouse=True)
def fresh_store():
    DatabaseManager.clear_planning_list("all")
    yield
    DatabaseManager.clear_planning_list("all")


def test_add_requires_site_and_tags():
    assert DatabaseManager.add_planning_entry("", "rem") is None
    assert DatabaseManager.add_planning_entry("dan", "  ") is None


def test_add_sets_fields_and_dedupes():
    entry = DatabaseManager.add_planning_entry("dan", "rem blue_hair", "rating:g", "check animu")
    assert entry["site"] == "dan" and entry["tags"] == "rem blue_hair"
    assert entry["rating"] == "rating:g" and entry["note"] == "check animu"
    assert entry["completed"] is False and entry["completed_at"] == 0
    assert entry["id"]
    # identical entry (site+tags+rating+note) is refused
    assert DatabaseManager.add_planning_entry("dan", "rem blue_hair", "rating:g", "check animu") is None
    # different rating still allowed
    second = DatabaseManager.add_planning_entry("dan", "rem blue_hair", "rating:g rating:q")
    assert second and second["rating"] == "rating:g rating:q"


def test_newest_first_ordering():
    a = DatabaseManager.add_planning_entry("dan", "one")
    b = DatabaseManager.add_planning_entry("kona", "two")
    ids = [e["id"] for e in DatabaseManager.load_planning_list()]
    assert ids == [b["id"], a["id"]]


def test_toggle_completion_stamp():
    entry = DatabaseManager.add_planning_entry("dan", "rem")
    out = DatabaseManager.update_planning_entry(entry["id"], {"completed": True})
    assert out["completed"] is True and out["completed_at"] > 0
    out = DatabaseManager.update_planning_entry(entry["id"], {"completed": False})
    assert out["completed"] is False and out["completed_at"] == 0
    # repeated True keeps the original hand-set date
    out = DatabaseManager.update_planning_entry(entry["id"], {"completed": True})
    stamp = out["completed_at"]
    out = DatabaseManager.update_planning_entry(entry["id"], {"completed": True})
    assert out["completed_at"] == stamp


def test_update_fields_and_guards():
    entry = DatabaseManager.add_planning_entry("dan", "rem")
    out = DatabaseManager.update_planning_entry(entry["id"], {"tags": "rem blue", "note": "later"})
    assert out["tags"] == "rem blue" and out["note"] == "later"
    assert DatabaseManager.update_planning_entry("nope", {"tags": "x"}) is None
    assert DatabaseManager.update_planning_entry(entry["id"], {"tags": "   "}) is None


def test_remove_and_clear():
    keep = DatabaseManager.add_planning_entry("dan", "kept")
    done = DatabaseManager.add_planning_entry("kona", "done")
    DatabaseManager.update_planning_entry(done["id"], {"completed": True})
    assert DatabaseManager.remove_planning_entry(keep["id"]) is True
    assert DatabaseManager.remove_planning_entry(keep["id"]) is False
    DatabaseManager.clear_planning_list("completed")
    left = DatabaseManager.load_planning_list()
    assert left == [] or all(not e.get("completed") for e in left)
    DatabaseManager.clear_planning_list()
    assert DatabaseManager.load_planning_list() == []


def test_clear_duplicates_keeps_first():
    a = DatabaseManager.add_planning_entry("gelbooru", "rem", "rating:g", "n")
    b = DatabaseManager.add_planning_entry("gelbooru", "rem", "rating:g", "n")
    assert b is None  # add() refuses exact duplicates
    other = DatabaseManager.add_planning_entry("kona", "rem")
    # forge a duplicate by updating 'other' onto 'a's identity
    DatabaseManager.update_planning_entry(other["id"],
                                          {"site": "gelbooru", "tags": "rem",
                                           "rating": "rating:g", "note": "n"})
    assert DatabaseManager.clear_planning_list("duplicates") == 1
    left = DatabaseManager.load_planning_list()
    assert len(left) == 1 and left[0]["id"] == a["id"]
    DatabaseManager.clear_planning_list()
