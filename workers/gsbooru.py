import os
import asyncio

try:
    from workers import BaseWorker
except ImportError:
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from workers import BaseWorker

from core.shared import (
    sanitize_path_component,
    sanitize_filename,
    safe_ensure_dir,
    media_subdir,
    TAG_TYPE_MAP,
)
from core.database import DatabaseManager, DATABASE_DIR

POSTS_API = "https://gsbooru.org/api/posts"
TAGS_API = "https://gsbooru.org/api/tags"
TAG_TYPES_FILE = os.path.join(DATABASE_DIR, "gsbooru_tag_types.json")

# API schema: rating is an integer {0: g, 1: s, 2: q, 3: e}
RATING_WORD_BY_INT = {0: "general", 1: "sensitive", 2: "questionable", 3: "explicit"}


class GsbooruWorker(BaseWorker):

    def __init__(self, tag, amount, rating, exclusions, net_config):
        super().__init__("gsbooru", "Gsbooru", amount, net_config)

        self.original_tag = tag.strip().lower()
        self.rating = rating
        self.exclusions = exclusions
        self.api_key = os.getenv("GSBOORU_API_KEY", "")
        # persistent across runs — known tags never refetch (verified types only)
        cached = DatabaseManager.load_json(TAG_TYPES_FILE)
        self.tag_cache = dict(cached) if isinstance(cached, dict) else {}
        self._last_api_launch = 0.0

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

        # live-verified: gsbooru's tags param has no '-' exclusion
        # ("1girl -bogus_xyz" -> 0 posts) — send positives only and drop
        # excluded tags per-post in the scrape loop
        parts = self.original_tag.split()
        self.exclude_tags = [t[1:] for t in parts if t.startswith("-") and t[1:]]
        self.api_tag = " ".join(t for t in parts if not t.startswith("-"))

        self.safe_tag_name = sanitize_path_component(self.api_tag, fallback="all")

        self.tag_dir = os.path.join(
            self.site_root,
            self.safe_tag_name
        )

        safe_ensure_dir(self.tag_dir)

    async def _api_get(self, params, url=POSTS_API):
        """GET a JSON API endpoint with Bearer auth. Returns the parsed dict,
        or None after logging a whitelisted 'API error' line (console-visible)."""
        if not self.api_key:
            self.log("API error: GSBOORU_API_KEY missing in .env — generate one in gsbooru account settings.")
            return None
        headers = {"Authorization": f"Bearer {self.api_key}"}
        last = "unknown error"
        loop = asyncio.get_running_loop()
        for attempt in range(4):
            if self.stop_event.is_set():
                return None
            # global pacing for this worker: every launch (posts + tags, all
            # concurrent tasks) ≥ 1.1 s after the previous one — the limit is
            # now 1 request/s, held just under it so jitter can't trip 429s
            while True:
                wait = self._last_api_launch + 1.1 - loop.time()
                if wait <= 0:
                    self._last_api_launch = loop.time()
                    break
                await asyncio.sleep(wait)
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
        """Run several GETs with up to 3 in flight (the server parallel-handles);
        launch pacing lives in _api_get so every call shares one gate."""
        sem = asyncio.Semaphore(3)

        async def one(p):
            async with sem:
                return await self._api_get(p, url=TAGS_API)

        return await asyncio.gather(*[one(p) for p in params_list])

    async def _fetch_tag_types(self, tag_names):
        had = len(self.tag_cache)
        try:
            await self._fetch_tag_types_impl(tag_names)
        finally:
            # ponytail: save even partial fetches (impl bails on API failure);
            # skip the write when nothing new landed
            if len(self.tag_cache) > had:
                await asyncio.to_thread(DatabaseManager.save_json, TAG_TYPES_FILE, self.tag_cache)

    async def _fetch_tag_types_impl(self, tag_names):
        """Categorize tags via batched prefix lookups on /api/tags (the server
        takes ~3 s per exact query, so group by first letter)."""
        remaining = {t for t in tag_names if t not in self.tag_cache}
        if not remaining:
            return
        by_char = {}
        for t in remaining:
            by_char.setdefault(t[0].lower(), []).append(t)
        # prefix pages: top 300 per first letter (3 rounds of parallel
        # calls) — much cheaper than one exact query per leftover
        if self.stop_event.is_set():
            return
        for page in (1, 2, 3):
            chars = [ch for ch in by_char
                     if any(n not in self.tag_cache for n in by_char[ch])]
            if not chars:
                break
            results = await self._throttled_get(
                [{"tag_string": f"{ch}*", "limit": 100, "page": page,
                  "sort": "post_count"}
                 for ch in chars])
            for ch, data in zip(chars, results):
                if data is None:
                    return
                wanted = {n.lower(): n for n in by_char[ch]
                          if n not in self.tag_cache}
                for t in data.get("tags") or []:
                    low = str(t.get("name", "")).lower()
                    if low in wanted:
                        orig = wanted.pop(low)
                        self.tag_cache[orig] = TAG_TYPE_MAP.get(t.get("type", 0), "tag")

        # final fallback: exact lookups for anything past rank 300
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
                # ponytail: failures/no-matches stay uncached (retried next run)
                if match is not None:
                    self.tag_cache[name] = TAG_TYPE_MAP.get(match.get("type", 0), "tag")

    def _excluded(self, tag_names):
        return any(t in self.exclude_tags for t in tag_names)

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
                    media_subdir(ext)
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
                if self._excluded(tag_names):
                    continue
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
