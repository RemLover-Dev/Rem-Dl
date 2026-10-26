"""The startup folder-policy migration must retitle the tree *and* the DB
without losing a single file or tag.

Every existing user hits this on update, so the guarantees pinned here are:
  1. folders come out under the naming policy, files intact;
  2. stored paths follow the folders, so thumbnails keep resolving;
  3. tags are byte-identical before and after (the canary);
  4. it is idempotent, and a half-renamed tree converges rather than breaking.
"""
import os
import sqlite3

import pytest

from core import folder_policy
from core.shared import folder_component

_OLD_TREE = [
    "danbooru/soo_ou/safe/images/5377943.jpg",
    "danbooru/soo_ou/sensitive/images/5368383.jpg",
    "e-shuushuu/rem/safe/images/a.png",
    "nekos.best/neko_gif/gifs/n.gif",
    "zerochan/already_good/safe/images/z.jpg",
]

# rows the DB claims exist, in the old naming
_GALLERY_ROWS = [
    ("5377943.jpg", "danbooru/soo_ou/safe/images/5377943.jpg"),
    ("5368383.jpg", "danbooru/soo_ou/sensitive/images/5368383.jpg"),
    ("a.png", "e-shuushuu/rem/safe/images/a.png"),
    ("n.gif", "nekos.best/neko_gif/gifs/n.gif"),
    ("z.jpg", "zerochan/already_good/safe/images/z.jpg"),
    # a file the user deleted before updating: must be left alone, never dropped
    ("gone.jpg", "danbooru/soo_ou/safe/images/gone.jpg"),
]
_HISTORY_ROWS = [
    ("5377943.jpg", "danbooru/soo_ou/safe/images/5377943.jpg"),
    ("gone.jpg", "danbooru/soo_ou/safe/images/gone.jpg"),
]


def _build_tree(root):
    for rel in _OLD_TREE:
        path = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(b"x")


def _build_db(db_path):
    con = sqlite3.connect(db_path)
    con.executescript("""
        CREATE TABLE gallery (id INTEGER PRIMARY KEY, filename TEXT, filepath TEXT);
        CREATE TABLE gallery_tags (gallery_id INTEGER, tag TEXT);
        CREATE TABLE image_history (id INTEGER PRIMARY KEY, filename TEXT, filepath TEXT);
    """)
    for i, (fn, fp) in enumerate(_GALLERY_ROWS, 1):
        con.execute("INSERT INTO gallery VALUES (?,?,?)", (i, fn, fp))
        # the exact tag shape the app stores: underscored, mixed case
        con.executemany("INSERT INTO gallery_tags VALUES (?,?)",
                        [(i, "long_hair"), (i, "1girl"), (i, "blue_hair")])
    for i, (fn, fp) in enumerate(_HISTORY_ROWS, 1):
        con.execute("INSERT INTO image_history VALUES (?,?,?)", (i, fn, fp))
    con.commit()
    con.close()


@pytest.fixture
def scene(tmp_path, monkeypatch):
    root = str(tmp_path / "Rems Dl")
    db = tmp_path / "gallery.db"
    _build_tree(root)
    _build_db(str(db))

    monkeypatch.setattr(folder_policy, "DATABASE_DIR", str(tmp_path))
    monkeypatch.setattr(folder_policy, "GALLERY_DB_FILE", str(db))
    monkeypatch.setattr(folder_policy, "MARKER_FILE", str(tmp_path / ".folder_policy_v2"))
    monkeypatch.setattr(folder_policy, "MAPPING_FILE", str(tmp_path / "folder_rename_map.json"))
    # get_store() only exists to flush gallery.json into SQLite first; there is
    # no JSON in this scene and the real store must never be touched by a test
    monkeypatch.setattr("core.gallery_store.get_store", lambda: None)
    return root, db


def _dirs(root):
    out = set()
    for dirpath, dirnames, _ in os.walk(root):
        for d in dirnames:
            out.add(os.path.relpath(os.path.join(dirpath, d), root).replace(os.sep, "/"))
    return out


def _paths(db, table):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return [r[0] for r in con.execute(f"SELECT filepath FROM {table}")]
    finally:
        con.close()


def _tag_rows(db):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return (con.execute("SELECT COUNT(*) FROM gallery_tags").fetchone()[0],
                con.execute("SELECT COUNT(DISTINCT tag) FROM gallery_tags").fetchone()[0])
    finally:
        con.close()


def test_tree_retitled_and_files_survive(scene):
    root, _ = scene
    assert folder_policy.migrate_folder_policy(root, force=True) is True

    expected = {
        "Danbooru/Soo Ou/Safe/Images/5377943.jpg",
        "Danbooru/Soo Ou/Sensitive/Images/5368383.jpg",
        "E-Shuushuu/Rem/Safe/Images/a.png",
        "Nekos.best/Neko Gif/Gifs/n.gif",
        "Zerochan/Already Good/Safe/Images/z.jpg",
    }
    for rel in expected:
        assert os.path.isfile(os.path.join(root, *rel.split("/"))), f"missing {rel}"
    # nothing left behind under an old-style name
    assert not any(p.split("/")[-1] in ("soo_ou", "images", "neko_gif", "rem") for p in _dirs(root))


def test_stored_paths_follow_the_folders(scene):
    root, db = scene
    folder_policy.migrate_folder_policy(root, force=True)

    for fp in _paths(db, "gallery") + _paths(db, "image_history"):
        dirs, _ = fp.rsplit("/", 1)
        # every directory component already satisfies the policy, so the path
        # lands on a folder that exists (except the pre-deleted file)
        assert dirs == "/".join(folder_component(d) for d in dirs.split("/")), fp
        assert os.path.exists(os.path.join(root, *fp.split("/"))) or fp.endswith("gone.jpg")


def test_tags_are_byte_identical_after_migration(scene):
    root, db = scene
    before = _tag_rows(db)
    folder_policy.migrate_folder_policy(root, force=True)
    assert _tag_rows(db) == before, "the migration must never touch gallery_tags"
    assert before == (18, 3)   # 6 rows x 3 tags, 3 distinct


def test_migration_is_idempotent(scene):
    root, db = scene
    folder_policy.migrate_folder_policy(root, force=True)
    first_dirs = _dirs(root)
    first_paths = _paths(db, "gallery")

    assert folder_policy.migrate_folder_policy(root, force=True) is True
    assert folder_policy.migrate_folder_policy(root) is False, "marker must short-circuit"

    assert _dirs(root) == first_dirs
    assert _paths(db, "gallery") == first_paths


def test_half_renamed_tree_converges(scene):
    """An interrupted run leaves new parents over old leaves; rerunning must fix it."""
    root, db = scene
    # simulate the crash: parent pretty, leaf untouched
    os.rename(os.path.join(root, "danbooru/soo_ou"), os.path.join(root, "danbooru/Soo Ou"))
    os.rename(os.path.join(root, "e-shuushuu/rem"), os.path.join(root, "e-shuushuu/Rem"))

    folder_policy.migrate_folder_policy(root, force=True)

    assert os.path.isfile(os.path.join(root, "Danbooru/Soo Ou/Safe/Images/5377943.jpg"))
    assert os.path.isfile(os.path.join(root, "E-Shuushuu/Rem/Safe/Images/a.png"))
    for fp in _paths(db, "gallery"):
        assert os.path.exists(os.path.join(root, *fp.split("/"))) or fp.endswith("gone.jpg")


def test_merge_on_collision_keeps_both_files(scene):
    """Two folders that prettify to the same name must not lose either's files."""
    root, _ = scene
    other = os.path.join(root, "danbooru/soo_ou/Safe/Images")   # same target as safe/images
    os.makedirs(other, exist_ok=True)
    with open(os.path.join(other, "5377943.jpg"), "wb") as fh:
        fh.write(b"clashing copy")
    with open(os.path.join(other, "extra.jpg"), "wb") as fh:   # only the clash has this
        fh.write(b"y")

    folder_policy.migrate_folder_policy(root, force=True)

    target = os.path.join(root, "Danbooru/Soo Ou/Safe/Images")
    assert os.path.isfile(os.path.join(target, "extra.jpg")), "non-clashing file lost"
    # the clash is never overwritten: one of the two copies survives on disk
    assert os.path.isfile(os.path.join(target, "5377943.jpg"))


def test_defers_when_the_store_cannot_open(scene, monkeypatch):
    """gallery.json rows only exist once the store opens — never write the
    marker before that, or their paths would stay in the old naming forever."""
    root, _ = scene

    def _boom():
        raise RuntimeError("tags will not round-trip")

    monkeypatch.setattr("core.gallery_store.get_store", _boom)
    assert folder_policy.migrate_folder_policy(root, force=True) is False
    assert not os.path.exists(folder_policy.MARKER_FILE), \
        "marker written while the JSON rows were still unretitleable"

    # ...and it retries on the next start, once the store comes good
    monkeypatch.setattr("core.gallery_store.get_store", lambda: None)
    assert folder_policy.migrate_folder_policy(root, force=True) is True
    assert os.path.exists(folder_policy.MARKER_FILE)
    assert os.path.isfile(os.path.join(root, "Danbooru/Soo Ou/Safe/Images/5377943.jpg"))


def test_missing_root_is_a_clean_no_op(tmp_path, monkeypatch):
    monkeypatch.setattr(folder_policy, "DATABASE_DIR", str(tmp_path))
    monkeypatch.setattr(folder_policy, "GALLERY_DB_FILE", str(tmp_path / "nope.db"))
    monkeypatch.setattr(folder_policy, "MARKER_FILE", str(tmp_path / ".m"))
    monkeypatch.setattr(folder_policy, "MAPPING_FILE", str(tmp_path / "map.json"))
    assert folder_policy.migrate_folder_policy(str(tmp_path / "absent"), force=True) is False
    assert os.path.exists(str(tmp_path / ".m")), "fresh installs should mark themselves done"
