"""Recover tags for gallery entries that a folder rescan only knew by folder name.

backfill_tags.py restores entries whose tags still sit inside the file's own
metadata. This covers the rest — files with no metadata at all: their md5
names the post on every site that answers an md5 search, so the full tag set
comes back from the API instead of the folder name.

A refetched post must prove it is ours (its md5 field, or an md5-bearing
file path) before a single tag is written — a site that ignores the query
must never mislabel an image.

danbooru is the exception: its filenames are post ids and our stored copies
are re-encodes (same or smaller dims, different bytes), so md5: never matches.
The id proves identity, and --fullsize swaps the bytes back to the original.

Bare entries self-heal after every rescan (Rems_Dl._heal_bare_gallery_tags),
so a file that ever lands on disk without a download-time gallery record gets
its tags on the next launch.

Run:  python3 -m core.md5_refetch [--limit N] [--dry]
"""
import argparse
import hashlib
import html
import os
import signal
import sys
import time
import urllib.parse
from collections import Counter

from core import shared

_DAPI = "dapi"
_MOEBORU = "moebooru"
_DANBOORU = "danbooru"

# what danbooru's Cloudflare accepts: an API client that says who it is
_API_UA = "RemsDlDesktopApp/1.0"


def _danbooru_id(img):
    """danbooru filenames are the post id ("<id>.jpg"), never md5-bearing."""
    stem = os.path.splitext(str(img.get("filename") or ""))[0]
    return stem if stem.isdigit() and len(stem) < 32 else None


def _query(site, md5, post_id=None):
    """(url, params, kind) for an md5 lookup, or None when the site has none."""
    tag = f"md5:{md5}"
    if site == "gelbooru":
        params = {"page": "dapi", "s": "post", "q": "index", "json": 1,
                  "limit": 3, "tags": tag}
        key, uid = os.getenv("GELBOORU_API_KEY", ""), os.getenv("GELBOORU_USER_ID", "")
        if key and uid:
            params.update({"api_key": key, "user_id": uid})
        return "https://gelbooru.com/index.php", params, _DAPI
    if site == "safebooru":
        return ("https://safebooru.org/index.php",
                {"page": "dapi", "s": "post", "q": "index", "json": 1,
                 "limit": 3, "tags": tag}, _DAPI)
    if site in ("yande", "konachan"):
        host = "https://yande.re" if site == "yande" else "https://konachan.com"
        return f"{host}/post.json", {"tags": tag, "limit": 3}, _MOEBORU
    if site == "danbooru":
        # our copies never match their md5, so the post id (from the filename)
        # is the only handle that finds the post
        params = {"tags": f"id:{post_id}" if post_id else tag, "limit": 3}
        key, login = os.getenv("DANBOORU_API_KEY", ""), os.getenv("DANBOORU_LOGIN", "")
        if key:
            params.update({"api_key": key, "login": login})
        return "https://danbooru.donmai.us/posts.json", params, _DANBOORU
    # gsbooru has no md5 lookup: tags=md5: and a md5= param are both
    # silently ignored (verified against a real md5 from its own index),
    # and filenames carry no post id — an empty answer there can never
    # prove absence, so the site is never asked
    return None


_dead_hosts = set()

# ^C lands inside curl_cffi's callbacks, which print it as "Exception ignored
# from cffi callback" and swallow it — the loop never sees it. A flag does.
# Set only by main()'s SIGINT handler: importing this module (the app's
# self-heal does) must never steal anyone else's Ctrl-C.
STOP = False


def _request_stop(signum, frame):
    global STOP
    STOP = True


def _mark_dead(host):
    if STOP:
        # the failure was ^C tearing the request down, not the host — never
        # condemn a healthy site on an interrupted probe
        raise KeyboardInterrupt
    _dead_hosts.add(host)

# consecutive "the API has no such post" answers before an entry stops being
# asked: two covers a transient empty reply (bad key, blip) without ever
# re-querying a post the site genuinely does not have
MISS_LIMIT = 2

# gallery is written this often while running — a Ctrl-C or a shutdown mid-run
# loses at most this many entries instead of the whole pass
CHECKPOINT_EVERY = 10


class HostDead(Exception):
    """Both the proxy and direct route to this host failed this run."""


def _get(url, params, bearer=None, parse="json", timeout=20):
    """GET with the configured proxy first, direct second. First failure of a
    host marks it dead — every later lookup on it skips the network instead of
    burning two timeouts per remaining image."""
    host = urllib.parse.urlparse(url).netloc
    if host in _dead_hosts:
        raise HostDead(host)
    from curl_cffi import requests
    verify = str(os.getenv("VERIFY_TLS", "true")).lower() not in ("0", "false", "no")
    proxy = (os.getenv("PROXY_URL")
             if str(os.getenv("USE_PROXY", "")).lower() in ("1", "true", "yes") else None)
    # proxy first, direct second — the direct route is what answers hosts the
    # proxy can't reach, so it is never skipped
    attempts = ([proxy] if proxy else []) + [None]
    headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
    # danbooru (API and CDN) answers an honest API UA with the key and turns
    # away both browser spoofing and curl's default UA
    browser = not host.endswith("donmai.us")
    if not browser:
        headers.setdefault("User-Agent", _API_UA)
    errors = []
    for px in attempts:
        shared.pace_wait()
        try:
            r = requests.get(url, params=params, headers=headers,
                             impersonate="chrome" if browser else None,
                             timeout=timeout, verify=verify, proxy=px)
            if r.status_code == 200:
                if parse != "json":
                    return r.content
                try:
                    return r.json()
                except ValueError:
                    # safebooru answers a query with no hits with an empty
                    # 200 body — that is "no posts", not a dead host
                    if not getattr(r, "content", None):
                        return []
                    raise
            errors.append(f"HTTP {r.status_code}")
        except Exception as e:
            errors.append(str(e)[:100])
    _mark_dead(host)
    raise HostDead(f"{host}: {'; '.join(errors)}")


def _posts(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("post", "posts", "data", "results", "items"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return []


def _is_ours(post, md5, post_id=None):
    """Accept only a post that names our file — a site that ignores the query
    must never hand us someone else's tags."""
    if post_id is not None:
        # the id came from our own filename; its md5 is expected to differ
        # (our copy is a re-encode), so the id is the identity proof
        return str(post.get("id") or "") == post_id
    # safebooru calls its md5 field "hash"
    got = post.get("md5") or post.get("hash")
    if got:
        return str(got).lower() == md5
    for key in ("file_url", "image", "image_url", "url", "filename", "path"):
        if md5 in str(post.get(key) or "").lower():
            return True
    return False


def _split(value):
    return [html.unescape(t) for t in str(value or "").split() if t.strip()]


def _buckets(post, site, caches):
    if any(str(k).startswith("tag_string_") for k in post):
        # danbooru hands back categories itself
        tags = {cat: [] for cat in shared.TAG_CATEGORIES}
        for field, cat in (("tag_string_artist", "artist"),
                           ("tag_string_character", "character"),
                           ("tag_string_copyright", "copyright"),
                           ("tag_string_meta", "metadata"),
                           ("tag_string_general", "tag")):
            tags[cat] = _split(post.get(field))
        return tags
    raw = post.get("tags")
    names = raw if isinstance(raw, list) else _split(raw)
    tags = {"tag": [t for t in names if t]}
    shared.recategorize_tags(tags, site, caches)
    return tags


def _count(tags):
    return sum(len(v) for v in tags.values() if isinstance(v, list))


def supports(site):
    """True when this site answers an md5 lookup — the only entries worth
    counting as work. Sites without one are never asked for anything."""
    return _query(shared.normalize_site(site), "0" * 32) is not None


def miss_count(img):
    try:
        return int(img.get("md5_misses") or 0)
    except (TypeError, ValueError):
        return 0


def worth_trying(img):
    """Bare, on an md5-capable site, and not already known to be absent."""
    return (is_bare(img) and supports(img.get("site", ""))
            and miss_count(img) < MISS_LIMIT)


def clear_stale_md5_misses(images):
    """Drop miss verdicts that could never mean anything.

    danbooru's md5-era misses: md5: cannot match a re-encode, and under id:
    a miss means something again. gsbooru's: its API has no md5 search at
    all, so every empty answer there was the query going nowhere."""
    dropped = 0
    for i in images:
        site = shared.normalize_site(i.get("site", ""))
        hopeless = ((site == _DANBOORU and _danbooru_id(i))
                    or site == "gsbooru")
        if hopeless and "md5_misses" in i:
            i.pop("md5_misses")
            dropped += 1
    return dropped


def is_bare(img):
    """Folder-name-only entry (or no tags at all) — the rescan signature."""
    tags = img.get("tags")
    if not isinstance(tags, dict):
        return True
    return _count(tags) <= 1


def md5_of(img):
    stem = os.path.splitext(str(img.get("filename", "")))[0]
    if len(stem) == 32 and all(c in "0123456789abcdefABCDEF" for c in stem):
        return stem.lower()
    path = os.path.join(shared.MASTER_FOLDER, img.get("filepath") or "")
    if not os.path.isfile(path):
        return None
    digest = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _embed(path, tags, site):
    ext = os.path.splitext(path)[1].lower()
    if ext not in shared.EMBEDDABLE or not os.path.isfile(path):
        return False
    shared.write_image_metadata(
        path, tags.get("tag", []), tags.get("artist", []), site,
        tags.get("character"), tags.get("copyright"), tags.get("metadata"),
        tags.get("outfit"), tags.get("group"), tags.get("hair"), tags.get("eyes"))
    return True


def _upgrade(path, post):
    """Swap a re-encoded local copy for the post's original bytes.

    Verified by md5 before the atomic replace: a CDN that served a degraded
    variant must never be allowed to overwrite the file it claims to fix."""
    url = post.get("file_url")
    if not url or not post.get("md5"):
        return False
    data = _get(url, {}, parse="content", timeout=300)
    if hashlib.md5(data).hexdigest() != str(post["md5"]).lower():
        return False
    tmp = path + ".fullsize"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    return True


def refetch_entries(entries, *, dry=False, embed=True, caches=None, progress=None,
                    checkpoint=None, fullsize=False):
    """Recover tags for entries whose only tag is the folder name.

    Mutates each entry's tags in place (the caller owns the gallery dict and
    saves it). Returns a Counter: refetched / unsupported / no_md5 /
    not_found / failed / empty. progress() gets one line per entry;
    checkpoint() is called every CHECKPOINT_EVERY mutations and again on the
    way out, so an interrupted run keeps what it already fetched."""
    stats = Counter()
    caches = shared.load_tag_caches() if caches is None else caches
    total = len(entries)
    started = time.monotonic()
    pending = 0
    interrupted = False

    def _commit():
        nonlocal pending
        if checkpoint and pending:
            checkpoint()
            pending = 0

    try:
        for idx, img in enumerate(entries, 1):
            if STOP:
                raise KeyboardInterrupt
            site = shared.normalize_site(img.get("site", ""))
            detail = ""
            md5 = md5_of(img)
            post_id = _danbooru_id(img) if site == _DANBOORU else None
            if not md5 and not post_id:
                outcome = "no_md5"
            else:
                query = _query(site, md5, post_id)
                if not query:
                    outcome = "unsupported"
                else:
                    url, params, _kind = query
                    try:
                        payload = _get(url, params)
                    except Exception as e:
                        outcome, detail = "failed", str(e)
                    else:
                        post = next((p for p in _posts(payload)
                                     if isinstance(p, dict) and _is_ours(p, md5, post_id)), None)
                        if post is None:
                            outcome = "not_found"
                            # posts that matched nothing = query not honored (or a
                            # blip); a second consecutive miss retires the entry —
                            # but only a bare candidate (a tagged entry that's gone
                            # needs no retiring: it is never asked again anyway)
                            if not dry and is_bare(img):
                                img["md5_misses"] = miss_count(img) + 1
                                pending += 1
                        else:
                            tags = _buckets(post, site, caches)
                            if not _count(tags):
                                outcome = "empty"
                            else:
                                outcome = "refetched"
                                if not dry:
                                    img.pop("md5_misses", None)
                                    img["tags"] = tags
                                    path = os.path.join(shared.MASTER_FOLDER,
                                                        img.get("filepath") or "")
                                    if fullsize:
                                        # fullsize mode keeps the bytes pristine
                                        # (embedding would change them and invite a
                                        # re-download on every future run)
                                        try:
                                            if md5 and md5 != str(post.get("md5") or "").lower() \
                                                    and _upgrade(path, post):
                                                stats["fullsize"] += 1
                                        except Exception as e:
                                            detail = str(e)
                                    elif embed:
                                        _embed(path, tags, site)
                                    pending += 1
            stats[outcome] += 1
            if pending >= CHECKPOINT_EVERY:
                _commit()
            if progress:
                progress(_line(idx, total, site, outcome, stats, started, detail))
    except KeyboardInterrupt:
        interrupted = True
    finally:
        _commit()
    if interrupted and progress:
        progress(f"interrupted — {sum(stats.values())} entries processed, progress saved")
    return stats


def _line(idx, total, site, outcome, stats, started, detail=""):
    rate = idx / max(time.monotonic() - started, 1e-6)
    line = (f"[{idx:>4}/{total}] {site:<12} {outcome:<12} "
            f"got {stats['refetched']:>4} | miss {stats['not_found']:>4} | "
            f"skip {stats['unsupported']:>4} | fail {stats['failed']:>3} | "
            f"{rate:>4.1f}/s")
    return f"{line}  {detail}" if detail else line


def refetch_gallery_bare(limit=None, dry=False, entries=None, progress=None,
                         fullsize=False):
    """Load the gallery, refetch every worth-trying entry, save it."""
    gallery = shared.load_gallery()
    if entries is None:
        entries = [i for i in gallery.get("images", []) if worth_trying(i)]
    else:
        entries = [i for i in entries if worth_trying(i)]
    if limit:
        entries = entries[:limit]
    # checkpointed saves, not one at the end: a shutdown mid-run keeps the
    # tags it already fetched instead of starting the next launch from zero
    stats = refetch_entries(entries, dry=dry, progress=progress, fullsize=fullsize,
                            checkpoint=None if dry else (lambda: shared.save_gallery(gallery)))
    return stats


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, default=None,
                        help="stop after N bare entries")
    parser.add_argument("--dry", action="store_true", help="report only")
    parser.add_argument("--fullsize", action="store_true",
                        help="also swap danbooru re-encodes for the original files")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    # the app loads .env at import; a standalone run has to do it itself
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
    progress = None if args.quiet else (lambda msg: print(msg, flush=True))
    # only the CLI installs this: the app imports the module and keeps its own
    # signal disposition (the flag stops us after the current entry, with a
    # checkpoint and a summary, instead of dying mid-write)
    signal.signal(signal.SIGINT, _request_stop)
    gallery = shared.load_gallery()
    clear_stale_md5_misses(gallery.get("images", []))
    bare = [i for i in gallery.get("images", []) if is_bare(i)]
    no_api = sum(1 for i in bare if not supports(i.get("site", "")))
    absent = sum(1 for i in bare
                 if supports(i.get("site", "")) and miss_count(i) >= MISS_LIMIT)
    recoverable = [i for i in bare if worth_trying(i)]
    todo = recoverable[:args.limit] if args.limit else recoverable
    # fullsize reaches beyond bare candidates: already-tagged danbooru copies
    # are re-encodes too, and their tags must not keep them from upgrading
    extra = []
    if args.fullsize:
        have = {i.get("filepath") or i.get("filename") for i in todo}
        extra = [i for i in gallery.get("images", [])
                 if shared.normalize_site(i.get("site", "")) == _DANBOORU
                 and _danbooru_id(i)
                 and (i.get("filepath") or i.get("filename")) not in have]
    rate = os.getenv("REQ_RATE_LIMIT", "4")
    print(f"{'[dry] ' if args.dry else ''}{len(todo)} to refetch of {len(recoverable)} "
          f"({len(bare)} bare: {no_api} no md5 API, {absent} checked absent) "
          f"— pacing at {rate} req/s", flush=True)
    if extra:
        print(f"+ {len(extra)} danbooru files checked for full-size originals",
              flush=True)
    started = time.monotonic()
    stats = refetch_entries([*todo, *extra], dry=args.dry, progress=progress,
                            fullsize=args.fullsize,
                            checkpoint=None if args.dry
                            else (lambda: shared.save_gallery(gallery)))
    elapsed = time.monotonic() - started
    print(f"done in {elapsed:.0f}s: " +
          ", ".join(f"{k} {v}" for k, v in sorted(stats.items())), flush=True)
    if stats.get("failed"):
        print("failed hosts are cut off for the rest of the run — run again to retry them",
              flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
