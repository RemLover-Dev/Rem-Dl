import os, hashlib
import asyncio
from workers import BaseWorker, sanitize_path_component, sanitize_filename, safe_ensure_dir
from core.shared import TAG_TYPE_MAP
from core.database import DatabaseManager, DATABASE_DIR

TAG_TYPES_FILE = os.path.join(DATABASE_DIR, "konachan_tag_types.json")


class KonachanWorker(BaseWorker):
    def __init__(self, tag, amount, rating, exclusions, net_config):
        super().__init__("kona", "Konachan", amount, net_config)
        self.original_tag = tag.strip().lower()
        self.rating = rating
        self.exclusions = exclusions
        # persistent across runs — known tags never refetch (verified types only)
        cached = DatabaseManager.load_json(TAG_TYPES_FILE)
        self.tag_cache = dict(cached) if isinstance(cached, dict) else {}

        self.rating_map = {"s": "Safe", "q": "Questionable", "e": "NSFW"}
        code_of = {"rating:s": "s", "rating:q": "q", "rating:e": "e"}
        codes = [code_of[p] for p in (self.rating or "").split() if p in code_of]
        # the dropdown collapses a full set to All — treat it the same defensively
        if len(codes) == len(self.rating_map):
            codes = []
        self.rating_allowed = set(codes)
        self.rating_display = ", ".join(self.rating_map[c] for c in codes)

        self.api_tag = self.original_tag
        # ponytail: moebooru has no comma-OR in the rating metatag — push the
        # filter server-side only for a single rating, multi is filtered locally
        if len(codes) == 1:
            self.api_tag = f"{self.original_tag} rating:{codes[0]}".strip()

        clean_tag = " ".join(t for t in self.original_tag.split() if not t.startswith('-'))
        self.safe_tag = sanitize_path_component(clean_tag, fallback="konachan")
        self.tag_dir = os.path.join(self.site_root, self.safe_tag)
        safe_ensure_dir(self.tag_dir)

    def get_tags(self):
        return [self.original_tag]

    async def download_image(self, url, filepath, filename, tags_list, artists=None):
        return await self.enqueue_download(url, filepath, filename, tags_list, artists or [])

    async def fetch_posts(self):
        await self.scraper_task()

    async def _fetch_tag_types(self, tag_names):
        # ponytail: cap per run — v1 cache migration drops unverified "tag"
        # entries; heal incrementally instead of stalling the first run
        uncached = [t for t in tag_names if t not in self.tag_cache][:150]
        if not uncached:
            return
        sem = asyncio.Semaphore(4)
        async def query_one(tag_name):
            async with sem:
                # ponytail: 3 attempts — a transient failure must not leave a
                # tag miscategorized for this whole run
                for attempt in range(3):
                    fetched = False
                    try:
                        resp = await self.session.get(
                            "https://konachan.com/tag.json",
                            params={"name": tag_name, "order": "count", "limit": 50}
                        )
                        if resp.status == 200:
                            tags = await resp.json() or []
                            # ponytail: scan every row for the exact tag
                            match = next((t for t in tags if str(t.get("name", "")).lower() == tag_name.lower()), None)
                            if match is not None:
                                self.tag_cache[tag_name] = TAG_TYPE_MAP.get(match.get("type", 0), "tag")
                            fetched = True  # 200 processed: match OR genuinely absent
                    except Exception:
                        fetched = False
                    if fetched:
                        break
                    await asyncio.sleep(0.5 * (attempt + 1))
                # ponytail: a tag that never comes back stays uncached —
                # caching a failure would mislabel it forever; next run retries
                await asyncio.sleep(0.2)
        await asyncio.gather(*[query_one(t) for t in uncached])
        # ponytail: concurrent workers may overwrite each other's save —
        # worst case those tags refetch on a later run
        DatabaseManager.save_json(TAG_TYPES_FILE, self.tag_cache)

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

    async def scraper_task(self):
        self.log(f"Initializing worker for tag: '{self.original_tag}'" + (f" (rating: {self.rating_display})" if self.rating_display else ""))

        auth = {}
        kona_user = os.getenv("KONACHAN_USERNAME", "")
        kona_pass = os.getenv("KONACHAN_PASSWORD", "")
        if kona_user and kona_pass:
            pw_hash = hashlib.sha1(f"So-I-Heard-You-Like-Mupkids-?--{kona_pass}--".encode()).hexdigest()
            auth = {"login": kona_user, "password_hash": pw_hash}
            self.log(f"Authenticating as '{kona_user}' (needed for questionable/explicit).")
        else:
            self.log("No Konachan credentials loaded - running as anonymous (safe content only).")

        collected_count = 0
        page = 1

        while not self.stop_event.is_set() and (self.amount == 0 or collected_count < self.amount):
            try:
                self.log(f"Scanning API... (Page {page})")
                limit_val = min(100, self.amount - collected_count if self.amount > 0 else 100)

                resp = await self.session.get("https://konachan.com/post.json", params={"tags": self.api_tag, "page": page, "limit": limit_val, **auth})
                if resp.status in [403, 429]:
                    self.log(f"ERROR {resp.status}. Change proxy.")
                    break
                resp.raise_for_status()

                text_resp = (await resp.text()).strip()
                if not text_resp or text_resp == "[]" or text_resp == "null":
                    if page == 1:
                        self.log(f"ZERO images found for '{self.api_tag}'.")
                        if self.rating_allowed & {"q", "e"}:
                            if auth:
                                self.log("Authenticated, got 0 non-safe posts. Check 'Show explicit content' is enabled in your konachan.com profile settings.")
                            else:
                                self.log("No Konachan credentials configured (Options tab) - non-safe content needs a logged-in account.")
                    break

                raw_data = await resp.json()
                if isinstance(raw_data, dict):
                    if "success" in raw_data and not raw_data["success"]:
                        self.log(f"API Alert: {raw_data.get('message', 'Unknown Error')}")
                        break
                    posts = [raw_data]
                elif isinstance(raw_data, list):
                    posts = raw_data
                else:
                    break

                if not posts:
                    break

            except Exception as e:
                err_str = str(e)
                if "403" in err_str:
                    self.log("ERROR 403: Cloudflare/ISP block. You need a proxy.")
                else:
                    self.log(f"API Error: {e}")
                await asyncio.sleep(5)
                continue

            had_valid = False

            all_tags = set()
            for post in posts:
                if isinstance(post, dict):
                    for t in post.get("tags", "").split():
                        all_tags.add(t.strip())
            if all_tags:
                await self._fetch_tag_types(all_tags)

            for post in posts:
                if self.stop_event.is_set() or (self.amount > 0 and collected_count >= self.amount):
                    break
                if not isinstance(post, dict):
                    continue

                post_rating = post.get("rating", "")
                if self.rating_allowed and post_rating not in self.rating_allowed:
                    continue

                url = post.get("file_url") or post.get("large_file_url")
                if not url:
                    continue

                ext = (post.get("file_ext") or "").lower()
                if ext in ["mp4", "webm", "zip"] and "-video" in self.exclusions:
                    continue
                if ext in ["jpg", "jpeg", "png", "webp"] and "-image" in self.exclusions:
                    continue
                if ext == "gif" and "-gif" in self.exclusions:
                    continue
                if ext not in ["jpg", "jpeg", "png", "gif", "webp", "mp4", "webm"]:
                    continue

                filename = sanitize_filename(f"{post.get('id')}.{ext}", fallback=f"kona_{post.get('id', 'item')}.jpg")
                rating_label = sanitize_path_component(self.rating_map.get(post_rating, "Unknown"), fallback="Unknown")
                rating_dir = os.path.join(self.tag_dir, rating_label, "images")
                safe_ensure_dir(rating_dir)
                filepath = os.path.join(rating_dir, filename)

                tags_raw = post.get("tags", "")
                tags_list = [t.strip() for t in tags_raw.split() if t.strip()]
                tags_list, artists, characters, copyrights, metadata_tags = self._categorize_tags(tags_list)

                if await self.enqueue_download(url, filepath, filename, tags_list, artists, characters, copyrights, metadata_tags):
                    collected_count += 1
                    had_valid = True

            page += 1
            if had_valid and not self.stop_event.is_set() and (self.amount == 0 or collected_count < self.amount):
                await asyncio.sleep(self.anti_ban_pause)

        actual = self.enqueued_count
        # ponytail: stopped runs wind down late — never paint summaries over the next run
        if self.stop_event.is_set():
            return
        if actual == 0:
            self.log("No new images to download.")
        else:
            self.check_amount_warning(actual)

    def run(self):
        asyncio.run(self.run_async_loop(self.scraper_task))

def worker_konachan(tag, amount, rating, exclusions, net_config):
    worker = KonachanWorker(tag, amount, rating, exclusions, net_config)
    worker.run()
