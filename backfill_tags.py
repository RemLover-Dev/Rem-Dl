"""Repair tag data between gallery.json and the metadata embedded in files.

The folder rescan rebuilt thousands of gallery entries carrying only the
folder name, while the full tag set still sits inside the jpg/png as a
Rems_Dl JPEG COM / PNG tEXt block. This walks both directions:

  file meta richer than gallery  -> gallery entry restored from the file
  gallery richer, file stale     -> categories healed from the worker tag
                                    caches, then re-embedded into the file

Run:  python3 backfill_tags.py [--dry]
"""
import os
import sys

from core import shared
from core.database import DatabaseManager

EMBEDDABLE = shared.EMBEDDABLE


def parse_meta(text):
    tags = {cat: [] for cat in shared.TAG_CATEGORIES}
    site = ""
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        key, val = key.strip().lower(), val.strip()
        if not val:
            continue
        if key == "site":
            site = val
        elif key in tags and val not in tags[key]:
            tags[key].append(val)
    return site, tags


def item_count(tags):
    return sum(len(v) for v in tags.values())


def norm_tags(d):
    """Order/empty-bucket-insensitive form for comparing tag dicts."""
    return {k: sorted(v) for k, v in (d or {}).items() if isinstance(v, list) and v}


def main(argv):
    dry = "--dry" in argv
    gallery = shared.load_gallery()
    caches = shared.load_tag_caches()
    restored = embedded = unrecoverable = skipped = site_fixed = recatted = 0

    for img in gallery.get("images", []):
        rel = img.get("filepath") or ""
        path = os.path.join(shared.MASTER_FOLDER, rel)
        gal_tags = img.get("tags") or {}
        if not os.path.exists(path):
            skipped += 1
            continue
        meta = shared.read_image_metadata(path)
        site, file_tags = parse_meta(meta) if meta else ("", {})
        if meta and item_count(file_tags) > item_count(gal_tags):
            img["tags"] = gal_tags = file_tags
            restored += 1
        if site:
            canon = shared.normalize_site(site)
            if canon != img.get("site"):
                img["site"] = canon
                site_fixed += 1
        # heal categories frozen at download time (failed / capped fetches)
        # once the worker caches carry the tag's real category
        if shared.recategorize_tags(gal_tags, img.get("site"), caches):
            recatted += 1
        ext = os.path.splitext(path)[1].lower()
        if norm_tags(file_tags) != norm_tags(gal_tags):
            if ext in EMBEDDABLE and (meta or item_count(gal_tags) > 1):
                # with file meta: refresh even a 1-tag set (category moved);
                # without: only embed real tag sets, not folder-name rebuilds
                if dry:
                    embedded += 1
                else:
                    t = gal_tags
                    shared.write_image_metadata(
                        path, t.get("tag", []), t.get("artist", []), img.get("site", ""),
                        t.get("character"), t.get("copyright"), t.get("metadata"),
                        t.get("outfit"), t.get("group"), t.get("hair"), t.get("eyes"),
                    )
                    if shared.read_image_metadata(path):
                        embedded += 1
                    else:
                        skipped += 1
            elif not meta and item_count(gal_tags) <= 1:
                unrecoverable += 1
            else:
                skipped += 1  # gallery is fine, file is a format we can't embed

    hist_recat = 0 if dry else DatabaseManager.recat_image_history(caches)
    if not dry and (restored or embedded or site_fixed or recatted or hist_recat):
        shared.save_gallery(gallery)
    print(f"{'[dry] ' if dry else ''}restored from file meta: {restored} | "
          f"recategorized: {recatted} (+{hist_recat} history) | "
          f"embedded into files: {embedded} | site corrected: {site_fixed} | "
          f"no tags anywhere: {unrecoverable} | skipped: {skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
