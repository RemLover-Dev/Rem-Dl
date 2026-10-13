"""One-time migration to the folder naming policy.

New downloads already land under the policy (`folder_component` in
core.shared: underscores become spaces, and the first letter at the start of a
name or directly after a space, '-' or '(' is capitalised). Existing installs
were downloaded with the old naming, so this module retitles the tree they
already have and rewrites the stored paths to match, otherwise every gallery
thumbnail would point at a folder that no longer exists.

It runs automatically at startup so a user receiving this update needs no
manual step, and it is safe to run twice:

  * a marker file next to the database makes a repeat run a no-op;
  * every rename is derived from the on-disk name, so an interrupted run can
    simply be rerun — a half-renamed tree converges instead of compounding;
  * nothing is ever deleted. When two folders want the same name the contents
    are merged and any file whose name already exists in the target is kept
    as it is, never overwritten.

Only the app's own download root is touched, never an arbitrary folder a user
points the app at — renaming unrelated trees is not something this should do
behind their back. The full old->new mapping is written next to the database so
the whole operation stays reversible.
"""
import json
import os
import shutil
import sqlite3

from core.gallery_store import DATABASE_DIR, GALLERY_DB_FILE

MARKER_FILE = os.path.join(DATABASE_DIR, ".folder_policy_v2")
MAPPING_FILE = os.path.join(DATABASE_DIR, "folder_rename_map.json")
# a table's path column to retitle
_PATH_TABLES = ("gallery", "image_history")


def _scan(root):
    """Every directory under root, as '/'-separated paths relative to it."""
    found = []
    for dirpath, dirnames, _ in os.walk(root):
        for d in dirnames:
            found.append(os.path.relpath(os.path.join(dirpath, d), root).replace(os.sep, "/"))
    return found


def _merge_dir(src, dst):
    """Move everything under src into dst, then drop the emptied src.

    A file whose name already exists in dst is left alone rather than
    overwritten — two different downloads sharing a name are both wanted.
    """
    moved = kept = 0
    for dirpath, _dirnames, filenames in os.walk(src):
        rel = os.path.relpath(dirpath, src)
        target = os.path.join(dst, rel) if rel != "." else dst
        os.makedirs(target, exist_ok=True)
        for name in filenames:
            s, d = os.path.join(dirpath, name), os.path.join(target, name)
            if os.path.exists(d):
                kept += 1
                continue
            shutil.move(s, d)
            moved += 1
    shutil.rmtree(src, ignore_errors=True)
    return moved, kept


def _rename_tree(root, folder_component):
    dirs = _scan(root)
    # deepest first: renaming a parent first would strand its children's paths
    todo = sorted(
        ((d, "/".join(folder_component(p) for p in d.split("/"))) for d in dirs),
        key=lambda kv: kv[0].count("/"), reverse=True)
    todo = [(old, new) for old, new in todo if old != new]

    renamed = merged = kept = skipped = 0
    for old, new in todo:
        src, dst = os.path.join(root, old), os.path.join(root, new)
        if not os.path.isdir(src):
            skipped += 1          # a parent merge already moved it, or it was deleted
            continue
        try:
            # deepest-first means the new parent may not exist yet; create it
            # so the rename has somewhere to land (it gets merged in below when
            # the parent's own turn comes round)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.exists(dst):
                kept += _merge_dir(src, dst)[1]
                merged += 1
            else:
                os.rename(src, dst)
                renamed += 1
        except OSError:
            # the folder is not going anywhere on this filesystem; leave it and
            # its stored path exactly as they are rather than half-doing it
            skipped += 1

    # the mapping is what makes this reversible, so write it even on a rerun
    mapping = {d: "/".join(folder_component(p) for p in d.split("/")) for d in _scan(root)}
    try:
        os.makedirs(DATABASE_DIR, exist_ok=True)
        with open(MAPPING_FILE, "w", encoding="utf-8") as fh:
            json.dump(mapping, fh, indent=1, sort_keys=True)
    except OSError:
        pass
    return renamed, merged, kept, skipped


def _retitle_paths(root, folder_component):
    """Rewrite every stored path by retitling its directory components.

    Derived from the stored path itself, not from what the rename run happened
    to touch, so it is correct whether a folder was renamed in this run or an
    earlier interrupted one.
    """
    if not os.path.exists(GALLERY_DB_FILE):
        return 0, 0

    con = sqlite3.connect(GALLERY_DB_FILE, timeout=30.0)
    try:
        con.execute("PRAGMA busy_timeout=30000")
        rewritten = unresolved = 0
        for table in _PATH_TABLES:
            rows = con.execute(
                f"SELECT rowid, filepath FROM {table} WHERE filepath IS NOT NULL").fetchall()
            updates = []
            for rowid, filepath in rows:
                parts = filepath.split("/")
                if len(parts) < 2:
                    continue
                *dirs, filename = parts
                new_path = "/".join([folder_component(d) for d in dirs] + [filename])
                if new_path != filepath:
                    updates.append((new_path, rowid))
                elif not os.path.exists(os.path.join(root, new_path)):
                    unresolved += 1
            if updates:
                con.executemany(f"UPDATE {table} SET filepath = ? WHERE rowid = ?", updates)
                con.commit()
                rewritten += len(updates)
        return rewritten, unresolved
    finally:
        con.close()


def migrate_folder_policy(root, force=False):
    """Retitle root's folder tree and the paths stored for it. No-op if the
    marker is present. Returns True when it ran."""
    if not force and os.path.exists(MARKER_FILE):
        return False
    if not os.path.isdir(root):
        # nothing downloaded yet; new folders are born under the policy anyway
        _touch_marker()
        return False

    # local import: core.shared imports other things at module scope and this
    # module is pulled in from the app's startup path
    from core.shared import folder_component

    # the gallery store migrates gallery.json into SQLite on first use; force
    # that here so rows that only exist in the JSON get retitled too. If it
    # refuses to open we must not write the marker — otherwise those rows would
    # be born with old paths after the chance to retitle them has gone.
    try:
        from core.gallery_store import get_store
        get_store()
    except Exception as exc:  # noqa: BLE001 — retry next start rather than strand rows
        print(f"Folder policy migration deferred (gallery store not ready): {exc}")
        return False

    try:
        renamed, merged, kept, skipped = _rename_tree(root, folder_component)
        rewritten, unresolved = _retitle_paths(root, folder_component)
    except Exception as exc:  # noqa: BLE001 — one bad folder must never block app startup
        print(f"Folder policy migration failed (will retry next start): {exc}")
        return False

    _touch_marker()
    if renamed or merged or rewritten:
        print(f"Folder naming policy applied: {renamed} folders renamed, "
              f"{merged} merged ({kept} name clashes kept as-is), "
              f"{rewritten} stored paths retitled, {skipped} skipped.")
        if unresolved:
            print(f"  {unresolved} stored paths still point at files that are not "
                  f"on disk (deleted before this update) — left untouched.")
    return True


def _touch_marker():
    try:
        os.makedirs(DATABASE_DIR, exist_ok=True)
        with open(MARKER_FILE, "w", encoding="utf-8") as fh:
            fh.write("folder naming policy v2\n")
    except OSError:
        pass


def reset_marker():
    """Testing helper — forget that the migration already ran."""
    try:
        os.remove(MARKER_FILE)
    except OSError:
        pass
