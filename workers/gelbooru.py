import asyncio
import html
import os

from core.database import DATABASE_DIR, DatabaseManager
from core.shared import TAG_TYPE_MAP, media_subdir
from workers import BaseWorker, folder_component, safe_ensure_dir, sanitize_filename

TAG_TYPES_FILE = os.path.join(DATABASE_DIR, "gelbooru_tag_types.json")

RATING_CODE_MAP = {"g": "general", "s": "sensitive", "q": "questionable", "e": "explicit"}
RATING_LABEL_MAP = {"general": "Safe", "sensitive": "Sensitive", "questionable": "Questionable", "explicit": "NSFW"}
_CODE_OF = {"rating:g": "g", "rating:s": "s", "rating:q": "q", "rating:e": "e",
            "rating:general": "g", "rating:sensitive": "s",
            "rating:questionable": "q", "rating:explicit": "e"}


def build_query(tag, rating):
    """One AND query string for the dapi `tags` param plus the allowed-rating
    set. Shared by the downloader and the tag watcher — single source of
    truth for how tags+ratings serialize."""
    original_tag = tag.strip().lower()
    codes = [_CODE_OF[p] for p in (rating or "").split() if p in _CODE_OF]
    rating_allowed = {RATING_CODE_MAP[c] for c in codes}
    # ponytail: gelbooru ANDs rating tags (g+s returns count 0, no OR keyword),
    # so a multi-rating subset is expressed by negating the complement instead
    api_tag = original_tag
    if codes and len(codes) < len(RATING_CODE_MAP):
        if len(codes) == 1:
            api_tag = f"{original_tag} rating:{RATING_CODE_MAP[codes[0]]}".strip()
        else:
            neg = " ".join(f"-rating:{RATING_CODE_MAP[c]}" for c in RATING_CODE_MAP if c not in codes)
            api_tag = f"{original_tag} {neg}".strip()
    rating_display = ", ".join(RATING_LABEL_MAP[RATING_CODE_MAP[c]] for c in codes)
    return api_tag, rating_allowed, rating_display


class GelbooruWorker(BaseWorker):
    def __init__(self, tag, amount, rating, exclusions, net_config):
        super().__init__("gelbooru", "Gelbooru", amount, net_config)
        self.original_tag = tag.strip().lower()
        self.rating = rating
        self.exclusions = exclusions
        self.rating_code_map = RATING_CODE_MAP
        self.rating_label_map = RATING_LABEL_MAP
        self.api_tag, self.rating_allowed, self.rating_display = build_query(self.original_tag, rating)

        clean_tag = " ".join(t for t in self.original_tag.split() if not t.startswith('-'))
        self.safe_tag_name = folder_component(clean_tag, fallback="gelbooru")
        self.tag_dir = os.path.join(self.site_root, self.safe_tag_name)
        safe_ensure_dir(self.tag_dir)

        # persistent across runs: verified tag types are stored, so known tags
        # never refetch (was re-querying up to 150 tags per page every run)
        cached = DatabaseManager.load_json(TAG_TYPES_FILE)
        self.tag_cache = dict(cached) if isinstance(cached, dict) else {}

    async def _fetch_tag_types(self, tag_names):
        api_key = os.getenv("GELBOORU_API_KEY", "")
        user_id = os.getenv("GELBOORU_USER_ID", "")
        # ponytail: fetch EVERY uncached tag of this page. The old [:150] cap
        # (v1-migration era) silently left the rest uncategorized — and that
        # state got frozen into image metadata, image_history and the gallery
        # at download time, so later cache heals never repaired those files

        async def attempt_one(tag_name):
            params = {"page": "dapi", "s": "tag", "q": "index", "name": tag_name, "json": 1, "limit": 50}
            if api_key and user_id:
                params["api_key"] = api_key
                params["user_id"] = user_id
            async with self.session.get("https://gelbooru.com/index.php", params=params) as resp:
                if resp.status != 200:
                    return False
                data = await resp.json()
                tags = data.get("tag") or []
                # ponytail: only trust the exact tag, never a near miss
                # gelbooru entity-encodes response names (kal&#039;tsit_...)
                match = next((t for t in tags if html.unescape(str(t.get("name", ""))).lower() == tag_name.lower()), None)
                if match is not None:
                    self.tag_cache[tag_name] = TAG_TYPE_MAP.get(match.get("type", 0), "tag")
                return True  # 200 processed: match OR genuinely absent

        await self._run_tag_fetch(tag_names, attempt_one, TAG_TYPES_FILE, sem_limit=32)
        still = [t for t in tag_names if t not in self.tag_cache]
        if still:
            self.log(f"⚠️ {len(still)} tag(s) still uncategorized after 3 retries "
                     f"— downloads this run will store them as general tags.")

    async def scraper_task(self):
        self.log(f"Initializing worker for tag: '{self.original_tag}'" + (f" (rating: {self.rating_display})" if self.rating_display else ""))
        api_key = os.getenv("GELBOORU_API_KEY", "")
        user_id = os.getenv("GELBOORU_USER_ID", "")

        collected_count = 0
        pid = 0

        consecutive_errors = 0
        retried_rate_limit = False
        made_dirs = set()
        while not self.stop_event.is_set() and (self.amount == 0 or collected_count < self.amount):
            try:
                self.log(f"Scanning API... (Page {pid})")
                limit_val = min(100, self.amount - collected_count if self.amount > 0 else 100)
                params = {"page": "dapi", "s": "post", "q": "index", "tags": self.api_tag, "pid": pid, "limit": limit_val, "json": 1}
                if api_key and user_id:
                    params["api_key"] = api_key
                    params["user_id"] = user_id

                async with self.session.get("https://gelbooru.com/index.php", params=params) as resp:
                    if resp.status == 401:
                        self.log("ERROR 401: Unauthorized! Enter API Key in Options.")
                        break
                    elif resp.status in [403, 429]:
                        # one transient rate-limit hit should not kill a long
                        # run — pause once, refetch the same page
                        if not retried_rate_limit:
                            retried_rate_limit = True
                            self.log(f"HTTP {resp.status} — waiting 30s, then retrying page {pid}")
                            await asyncio.sleep(30)
                            continue
                        self.log(f"ERROR {resp.status}. Change proxy or slow down.")
                        break

                    resp.raise_for_status()
                    data = await resp.json()

                posts = data.get("post", [])

                if not posts:
                    if pid == 0: self.log(f"ZERO images found for '{self.original_tag}'.")
                    else: self.log("End of database reached.")
                    break

            except Exception as e:
                self.log(f"API Error: {e}")
                consecutive_errors += 1
                if consecutive_errors >= 3:
                    self.log("API failed 3 times in a row — giving up.")
                    break
                await asyncio.sleep(5)
                continue

            consecutive_errors = 0
            retried_rate_limit = False
            all_tags = set()
            for post in posts:
                if isinstance(post, dict):
                    # post tags arrive entity-encoded (kal&#039;tsit_...) — normalize before cache lookup
                    for t in post.get("tags", "").split():
                        all_tags.add(html.unescape(t.strip()))
            if all_tags:
                await self._fetch_tag_types(all_tags)

            had_valid = False
            for post in posts:
                if self.stop_event.is_set() or (self.amount > 0 and collected_count >= self.amount): break
                if not isinstance(post, dict): continue

                post_rating = post.get("rating", "")
                if self.rating_allowed and post_rating not in self.rating_allowed:
                    continue

                file_url = post.get("file_url", "")
                if not file_url: continue

                # strip the query first: "file.jpg?client=1" must yield jpg,
                # not "jpg?client=1" (exclusion filters then never match)
                ext = file_url.split('?')[0].split('.')[-1].lower()
                if ext in ["mp4", "webm", "zip"] and "-video" in self.exclusions: continue
                if ext in ["jpg", "jpeg", "png", "webp"] and "-image" in self.exclusions: continue
                if ext == "gif" and "-gif" in self.exclusions: continue

                raw_filename = file_url.split('/')[-1].split('?')[0]
                filename = sanitize_filename(raw_filename, fallback=f"gelbooru_{post.get('id', 'item')}.jpg")
                rating_label = folder_component(self.rating_label_map.get(post_rating, "Unknown"), fallback="Unknown")

                raw_tags = [html.unescape(t.strip()) for t in post.get("tags", "").split() if t.strip()]
                tags_list, artists, characters, copyrights, metadata_tags = self._categorize_tags(raw_tags)

                rating_dir = os.path.join(self.tag_dir, rating_label, media_subdir(ext))
                if rating_dir not in made_dirs:
                    safe_ensure_dir(rating_dir)
                    made_dirs.add(rating_dir)
                filepath = os.path.join(rating_dir, filename)

                if await self.enqueue_download(file_url, filepath, filename, tags_list, artists, characters, copyrights, metadata_tags, source_md5=post.get("md5")):
                    collected_count += 1
                    had_valid = True

            pid += 1
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


def worker_gelbooru(tag, amount, rating, exclusions, net_config):
    worker = GelbooruWorker(tag, amount, rating, exclusions, net_config)
    worker.run()
