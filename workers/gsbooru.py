import os
import re
import asyncio
import subprocess
import hashlib

try:
    from workers import BaseWorker
except ImportError:
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from workers import BaseWorker

from core.shared import (
    save_history,
    write_image_metadata,
    add_to_gallery,
    send_tags,
    build_tagd,
    check_duplicate,
    MASTER_FOLDER,
    sanitize_path_component,
    sanitize_filename,
    safe_ensure_dir,
    load_tag_cache,
    save_tag_cache,
    TAG_TYPE_MAP,
)

POSTS_API = "https://gsbooru.org/api/posts"
TAGS_API = "https://gsbooru.org/api/tags"

# API schema: rating is an integer {0: g, 1: s, 2: q, 3: e}
RATING_WORD_BY_INT = {0: "general", 1: "sensitive", 2: "questionable", 3: "explicit"}


def _gsbooru_curl(url, out_path=None, timeout=60):
    """Fetch file bytes via curl. No Referer header: /files/* answers 403
    when one is present (verified live — the old 'workaround' is now the block)."""
    cmd = ["curl", "-s", "-L", "--max-time", str(timeout)]
    if out_path:
        cmd += ["-o", out_path]
    cmd += [url]
    return subprocess.run(cmd, capture_output=True, timeout=timeout + 30)


class GsbooruWorker(BaseWorker):

    def __init__(self, tag, amount, rating, exclusions, net_config):
        super().__init__("gsbooru", "Gsbooru", amount, net_config)

        self.original_tag = tag.strip().lower()
        self.rating = rating
        self.exclusions = exclusions
        self.api_key = os.getenv("GSBOORU_API_KEY", "")
        self.tag_cache = load_tag_cache("gsbooru")

        # UI sends rating:g / rating:s / rating:q (space-separated when multi).
        # Site has General, Sensitive, and Questionable (rating=e is empty live).
        self.rating_label_map = {
            "general": "Safe",
            "sensitive": "Sensitive",
            "questionable": "Questionable",
            "explicit": "NSFW"
        }
        code_of = {"rating:g": "g", "rating:s": "s", "rating:q": "q",
                   "rating:general": "g", "rating:sensitive": "s",
                   "rating:questionable": "q"}
        code_word = {"g": "general", "s": "sensitive", "q": "questionable"}
        codes = [code_of[p] for p in (self.rating or "").split() if p in code_of]

        # ponytail: gsbooru ORs comma lists in its rating param (verified live:
        # rating=g,s -> mixed page of g and s); all three = the site's entirety,
        # same as All, so no param and no client-side filter
        if codes and len(codes) < 3:
            self.filter_code = ",".join(codes)
            self.filter_words = {code_word[c] for c in codes}
        else:
            self.filter_code = ""
            self.filter_words = set()
        self.rating_display = ", ".join(self.rating_label_map[code_word[c]] for c in codes)

        self.api_tag = self.original_tag

        clean_tag = " ".join(
            t for t in self.original_tag.split()
            if not t.startswith("-")
        )

        self.safe_tag_name = sanitize_path_component(clean_tag, fallback="all")

        self.tag_dir = os.path.join(
            self.site_root,
            self.safe_tag_name
        )

        safe_ensure_dir(self.tag_dir)

    def get_tags(self):
        return [self.original_tag]

    async def download_image(
        self,
        url,
        filepath,
        filename,
        tags_list,
        artists=None
    ):
        return await self.enqueue_download(
            url,
            filepath,
            filename,
            tags_list,
            artists or []
        )

    async def fetch_posts(self):
        await self.scraper_task()

    async def _api_get(self, params, url=POSTS_API):
        """GET a JSON API endpoint with Bearer auth. Returns the parsed dict,
        or None after logging a whitelisted 'API error' line (console-visible)."""
        if not self.api_key:
            self.log("API error: GSBOORU_API_KEY missing in .env — generate one in gsbooru account settings.")
            return None
        headers = {"Authorization": f"Bearer {self.api_key}"}
        last = "unknown error"
        for attempt in range(4):
            if self.stop_event.is_set():
                return None
            try:
                async with self.session.get(
                        url, params=params, headers=headers, timeout=30) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        if isinstance(data, dict) and data.get("status") == "ok":
                            return data
                        last = str(data)[:200]
                    elif resp.status == 401:
                        self.log("API error: auth failed (401) — check GSBOORU_API_KEY in .env.")
                        return None
                    elif resp.status == 400 and "rating" in params:
                        # server rejected the comma list (undocumented) — drop it;
                        # the client-side filter still enforces the selection
                        self.log("Notice: API rejected the rating parameter — filtering locally instead.")
                        params.pop("rating", None)
                        continue
                    elif resp.status == 429:
                        last = "429 rate limited"
                    else:
                        last = f"HTTP {resp.status}: {(await resp.text())[:200]}"
            except Exception as e:
                last = str(e)[:200]
            await asyncio.sleep(5)
        self.log(f"API error: {last}")
        return None

    async def _throttled_get(self, params_list):
        """Run several GETs with launches 1.7 s apart (the API allows 3 queries
        / 5 s per key) and up to 3 in flight (the server parallel-handles)."""
        sem = asyncio.Semaphore(3)

        async def one(p):
            async with sem:
                return await self._api_get(p, url=TAGS_API)

        tasks = []
        for p in params_list:
            tasks.append(asyncio.create_task(one(p)))
            await asyncio.sleep(1.7)
        return await asyncio.gather(*tasks)

    async def _fetch_tag_types(self, tag_names):
        """Categorize tags via batched prefix lookups on /api/tags (the server
        takes ~3 s per exact query, so group by first letter), cached on disk."""
        remaining = {t for t in tag_names if t not in self.tag_cache}
        if not remaining:
            return
        by_char = {}
        for t in remaining:
            by_char.setdefault(t[0].lower(), []).append(t)
        try:
            # wave 1: one prefix page per first letter (top 100 by post_count)
            if self.stop_event.is_set():
                return
            chars = list(by_char)
            results = await self._throttled_get(
                [{"tag_string": f"{ch}*", "limit": 100, "sort": "post_count"}
                 for ch in chars])
            for ch, data in zip(chars, results):
                if data is None:
                    return
                wanted = {n.lower(): n for n in by_char[ch]}
                for t in data.get("tags") or []:
                    low = str(t.get("name", "")).lower()
                    if low in wanted:
                        orig = wanted.pop(low)
                        self.tag_cache[orig] = TAG_TYPE_MAP.get(t.get("type", 0), "tag")

            # wave 2: leftovers outside the top-100 prefix pages get exact lookups
            leftovers = [n for n in remaining if n not in self.tag_cache]
            if leftovers and not self.stop_event.is_set():
                results = await self._throttled_get(
                    [{"tag_string": n, "limit": 50} for n in leftovers])
                for name, data in zip(leftovers, results):
                    if data is None:
                        return
                    match = next(
                        (t for t in data.get("tags") or []
                         if str(t.get("name", "")).lower() == name.lower()),
                        None)
                    self.tag_cache[name] = (
                        TAG_TYPE_MAP.get(match.get("type", 0), "tag")
                        if match is not None else "tag"
                    )
        finally:
            save_tag_cache(self.tag_cache, "gsbooru")

    def _categorize_tags(self, tag_names):
        artists, characters, copyrights, metadata_tags, general = [], [], [], [], []
        for t in tag_names:
            cat = self.tag_cache.get(t, "tag")
            if cat == "artist": artists.append(t)
            elif cat == "character": characters.append(t)
            elif cat == "copyright": copyrights.append(t)
            elif cat == "metadata": metadata_tags.append(t)
            else: general.append(t)
        return general, artists, characters, copyrights, metadata_tags

    async def enqueue_download(self, url, filepath, filename, tags_list, artists=None, characters=None, copyrights=None, metadata_tags=None, outfits=None, groups=None, hair=None, eyes=None):
        # gsbooru fetches files via curl too; skip the aiohttp HEAD request.
        if artists is None:
            artists = []
        if filename in self.dl_history or filename in self.queued_items or os.path.exists(filepath):
            return False
        self.queued_items.add(filename)
        self.download_queue.put_nowait((url, filepath, filename, tags_list, artists, 0, characters, copyrights, metadata_tags, outfits, groups, hair, eyes))
        self.enqueued_count += 1
        return True

    async def _async_download_file(self, url, filepath, filename, tags_list, artists, file_size=0, characters=None, copyrights=None, metadata_tags=None, outfits=None, groups=None, hair=None, eyes=None):
        if self.stop_event.is_set():
            self.enqueued_count -= 1
            return False

        part_path = filepath + ".part"
        for attempt in range(self.dl_retries):
            try:
                proc = await asyncio.to_thread(_gsbooru_curl, url, part_path, 300)
                if proc.returncode != 0:
                    raise Exception(
                        f"curl exit {proc.returncode}: "
                        f"{(proc.stderr or b'')[:200].decode('utf-8', 'replace')}"
                    )
                if not os.path.exists(part_path) or os.path.getsize(part_path) == 0:
                    raise Exception("empty download")

                # booru filenames embed their md5 (-<32hex>.ext); verify content
                # so a stale/recompressed variant is rejected, not saved as good
                m = re.search(r'-([0-9a-f]{32})\.[^.]+$', filename, re.I)
                if m:
                    h = hashlib.md5()
                    with open(part_path, 'rb') as f:
                        for chunk in iter(lambda: f.read(1 << 20), b''):
                            h.update(chunk)
                    if h.hexdigest() != m.group(1).lower():
                        os.remove(part_path)
                        raise Exception("md5 mismatch: server sent a different/degraded file")

                # publish under the real name only after full verification
                os.replace(part_path, filepath)

                # persistent perceptual-hash dedup (see core/shared.py)
                dup = check_duplicate(filepath, self.name)
                if dup is not None and dup.is_duplicate:
                    try:
                        os.remove(filepath)
                    except OSError:
                        pass
                    self.enqueued_count -= 1
                    self.log(f"[SKIP] Duplicate of {dup.matched_path or 'previous download'} — {filename} not saved")
                    return False

                self.downloaded_count += 1
                self.downloaded_bytes += os.path.getsize(filepath)
                self.dl_history.add(filename)
                save_history(self.site_root, self.dl_history)

                if self.is_scanning and self.amount > 0:
                    target_total = max(self.amount, self.enqueued_count)
                else:
                    target_total = max(self.enqueued_count, self.downloaded_count)

                pct = int((self.downloaded_count / target_total) * 100) if target_total > 0 else 0

                rel_path = os.path.relpath(filepath, MASTER_FOLDER)
                top_tags = ", ".join(tags_list[:5]) if tags_list else "No tags"
                tagd = build_tagd(artists, characters, copyrights, metadata_tags, outfits, groups, hair, eyes, tags_list)

                write_image_metadata(filepath, tags_list, artists, self.name, characters, copyrights, metadata_tags, outfits, groups, hair, eyes)
                add_to_gallery(self.name, filename, rel_path, tags_list, artists, characters, copyrights, metadata_tags, outfits, groups, hair, eyes)
                self.log(
                    f"[SUCCESS] Downloaded {filename} "
                    f"({self.downloaded_count}/{target_total}) [{pct}%] "
                    f"|PATH| {rel_path} |TAGS| {top_tags} |TAGD| {tagd}"
                )
                send_tags(self.name, filename, tags_list, artists, rel_path, characters, copyrights, metadata_tags, outfits, groups, hair, eyes)
                return True

            except Exception as e:
                if os.path.exists(part_path):
                    os.remove(part_path)
                if self.stop_event.is_set():
                    self.enqueued_count -= 1
                    break
                if attempt < self.dl_retries - 1:
                    await asyncio.sleep(2)
                else:
                    self.enqueued_count -= 1
                    err_msg = str(e).strip() or "HTTP 404 / File Deleted from Server"
                    self.log(f"[FAILED] {filename}: {err_msg}")
                    self.failed_count += 1
                    if os.path.exists(filepath):
                        os.remove(filepath)
        return False

    async def scraper_task(self):

        self.log(
            f"Initializing worker for tag: '{self.api_tag}'"
            + (f" (rating: {self.rating_display})" if self.rating_display else "")
        )

        page = 1

        # Scan page by page until the requested number of UNIQUE
        # images has been enqueued.
        while (
            not self.stop_event.is_set()
            and (
                self.amount == 0
                or self.downloaded_count < self.amount
            )
        ):

            params = {
                "tags": self.api_tag,
                "page": page,
                "limit": 100,
            }

            if self.filter_code:
                params["rating"] = self.filter_code

            data = await self._api_get(params)

            if data is None:
                break

            posts = data.get("posts") or []

            if not posts:
                if page == 1:
                    self.log(
                        f"ZERO images found for "
                        f"'{self.original_tag}'."
                    )
                else:
                    self.log("End of database reached.")
                break

            page_enqueued = 0

            for post in posts:

                if (
                    self.stop_event.is_set()
                    or (
                        self.amount > 0
                        and (
                            self.downloaded_count
                            + page_enqueued
                            >= self.amount
                        )
                    )
                ):
                    break

                if not isinstance(post, dict):
                    continue

                if post.get("is_soft_deleted"):
                    continue

                word = RATING_WORD_BY_INT.get(post.get("rating"))

                if (
                    self.filter_words
                    and word not in self.filter_words
                ):
                    continue

                file_url = post.get("file_url")
                if not file_url:
                    continue

                ext = (
                    (post.get("file_ext") or "")
                    .lower()
                    .lstrip(".")
                    or file_url.split("?")[0].split(".")[-1].lower()
                )

                if (
                    ext in ["mp4", "webm", "zip"]
                    and "-video" in self.exclusions
                ):
                    continue

                if (
                    ext in ["jpg", "jpeg", "png", "webp"]
                    and "-image" in self.exclusions
                ):
                    continue

                if (
                    ext == "gif"
                    and "-gif" in self.exclusions
                ):
                    continue

                if ext not in [
                    "jpg", "jpeg", "png", "gif",
                    "webp", "mp4", "webm"
                ]:
                    continue

                post_id = post.get("id")
                raw_filename = (
                    file_url.split("/")[-1].split("?")[0]
                )
                filename = sanitize_filename(
                    raw_filename,
                    fallback=f"gsbooru_{post_id}.{ext}"
                )

                rating_label = sanitize_path_component(
                    self.rating_label_map.get(
                        word,
                        "Unknown"
                    ),
                    fallback="Unknown"
                )

                rating_dir = os.path.join(
                    self.tag_dir,
                    rating_label,
                    "images"
                )

                safe_ensure_dir(rating_dir)

                filepath = os.path.join(
                    rating_dir,
                    filename
                )

                # cheap dupe check first — don't pay tag lookups for posts
                # we won't enqueue anyway (enqueue re-checks below)
                if (filename in self.dl_history
                        or filename in self.queued_items
                        or os.path.exists(filepath)):
                    continue

                # ponytail: the API returns one flat tag_string —
                # categories come from the cached /api/tags lookups
                tag_names = [
                    t
                    for t in str(post.get("tag_string", "")).split()
                    if t
                ]
                await self._fetch_tag_types(tag_names)
                (tags_list, artists, characters,
                 copyrights, metadata_tags) = self._categorize_tags(tag_names)

                if await self.enqueue_download(
                    file_url,
                    filepath,
                    filename,
                    tags_list,
                    artists,
                    characters,
                    copyrights,
                    metadata_tags
                ):
                    page_enqueued += 1

            # Wait for this page's downloads and pHash
            # deduplication to finish.
            if page_enqueued:
                await self.download_queue.join()

            page += 1

            total_pages = data.get("total_pages")

            if total_pages and page > total_pages:
                self.log("End of database reached.")
                break

            if (
                not self.stop_event.is_set()
                and (
                    self.amount == 0
                    or self.downloaded_count < self.amount
                )
            ):
                # API limit: 3 queries / 5 s — never go faster
                await asyncio.sleep(
                    max(self.anti_ban_pause, 1.7)
                )

        actual = self.enqueued_count

        # ponytail: stopped runs wind down late — never paint summaries over the next run
        if self.stop_event.is_set():
            return

        if actual == 0:

            self.log(
                "No new images to download."
            )

        else:

            self.check_amount_warning(
                actual
            )

    def run(self):
        asyncio.run(
            self.run_async_loop(
                self.scraper_task
            )
        )



def worker_gsbooru(
    tag,
    amount,
    rating,
    exclusions,
    net_config
):
    worker = GsbooruWorker(
        tag,
        amount,
        rating,
        exclusions,
        net_config
    )

    worker.run()
