import os
import asyncio
import xml.etree.ElementTree as ET
from workers import BaseWorker, sanitize_path_component, sanitize_filename, safe_ensure_dir
import core.shared as shared
from core.database import DatabaseManager, DATABASE_DIR

TAG_TYPES_FILE = os.path.join(DATABASE_DIR, "yande_tag_types.json")


class YandeWorker(BaseWorker):
    def __init__(self, tag, amount, rating, net_config):
        super().__init__("yande", "Yande.re", amount, net_config)
        self.original_tag = tag.strip().lower()
        self.rating = rating

        self.rating_map = {"s": "Safe", "q": "Questionable", "e": "NSFW"}
        code_of = {"rating:s": "s", "rating:q": "q", "rating:e": "e"}
        codes = [code_of[p] for p in (self.rating or "").split() if p in code_of]
        # the dropdown collapses a full set to All — treat it the same defensively
        if len(codes) == len(self.rating_map):
            codes = []
        self.rating_allowed = set(codes)
        self.rating_display = ", ".join(self.rating_map[c] for c in codes)

        self.api_tag = self.original_tag
        # ponytail: moebooru has no comma-OR in the rating metatag (verified live —
        # 'rating:s,rating:e' returns only the first) — a single rating goes
        # server-side, multi is filtered locally
        if len(codes) == 1:
            self.api_tag = f"{self.original_tag} rating:{codes[0]}".strip()

        FORMAT_WORDS = {"video", "image"}
        clean_tag = " ".join(t for t in self.original_tag.split() if not t.startswith('-') and t not in FORMAT_WORDS)
        self.safe_tag = sanitize_path_component(clean_tag, fallback="yande")
        self.tag_dir = os.path.join(self.site_root, self.safe_tag)
        safe_ensure_dir(self.tag_dir)
        # persistent across runs — known tags never refetch (verified types only)
        cached = DatabaseManager.load_json(TAG_TYPES_FILE)
        self.tag_cache = dict(cached) if isinstance(cached, dict) else {}

    def get_tags(self):
        return [self.original_tag]

    async def download_image(self, url, filepath, filename, tags_list, artists=None):
        return await self.enqueue_download(url, filepath, filename, tags_list, artists or [])

    async def fetch_posts(self):
        await self.scraper_task()

    async def _fetch_tag_types(self, tag_names):
        cache = self.tag_cache
        # ponytail: cap per run — uncached tags page through on later calls
        # instead of stalling the first run
        uncached = [t for t in tag_names if t not in cache][:150]
        if uncached:
            self.log(f"Fetching types for {len(uncached)} tags...")
            # ponytail: concurrent like gelbooru/safebooru — sequential
            # per-tag requests stalled every page
            sem = asyncio.Semaphore(4)
            async def query_one(tag_name):
                async with sem:
                    # ponytail: 3 attempts — a transient failure must not leave
                    # a tag miscategorized for this whole run
                    for attempt in range(3):
                        fetched = False
                        try:
                            resp = await self.session.get("https://yande.re/tag.xml", params={
                                "name": tag_name, "limit": 50
                            })
                            if resp.status == 200:
                                text = await resp.text()
                                root = ET.fromstring(text)
                                # ponytail: name= matches substrings and the exact
                                # row can bury below row 1, so scan every row
                                for tag_el in root.findall("tag"):
                                    if tag_el.get("name", "").lower() == tag_name.lower():
                                        cache[tag_name] = int(tag_el.get("type", 0))
                                        break
                                fetched = True  # 200 processed: match OR genuinely absent
                        except Exception:
                            fetched = False
                        if fetched:
                            break
                        await asyncio.sleep(0.5 * (attempt + 1))
                    # ponytail: a tag that never comes back stays uncached —
                    # caching a failure would mislabel it forever
                    await asyncio.sleep(0.2)
            await asyncio.gather(*[query_one(t) for t in uncached])
            # ponytail: concurrent workers may overwrite each other's save —
            # worst case those tags refetch on a later run
            DatabaseManager.save_json(TAG_TYPES_FILE, cache)
        return cache

    def _categorize_tags(self, tag_names, cache):
        result = {"artist": [], "character": [], "copyright": [], "metadata": [], "tag": []}
        for t in tag_names:
            tag_type = cache.get(t, 0)
            category = shared.TAG_TYPE_MAP.get(tag_type, "tag")
            result[category].append(t)
        return result

    async def scraper_task(self):
        self.log(f"Initializing worker for tag: '{self.original_tag}'" + (f" (rating: {self.rating_display})" if self.rating_display else ""))

        collected_count = 0
        page = 1

        while not self.stop_event.is_set() and (self.amount == 0 or collected_count < self.amount):
            try:
                self.log(f"Scanning API... (Page {page})")
                limit_val = min(100, self.amount - collected_count if self.amount > 0 else 100)

                resp = await self.session.get("https://yande.re/post.json", params={"tags": self.api_tag, "page": page, "limit": limit_val})
                if resp.status in [403, 429]:
                    self.log(f"ERROR {resp.status}. Change proxy.")
                    break
                resp.raise_for_status()

                text_resp = (await resp.text()).strip()
                if not text_resp or text_resp == "[]":
                    if page == 1:
                        self.log(f"ZERO images found for '{self.api_tag}'.")
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

            all_tag_names = set()
            for post in posts:
                if not isinstance(post, dict):
                    continue
                tags_raw = post.get("tags", "")
                all_tag_names.update(t.strip() for t in tags_raw.split() if t.strip())

            cache = await self._fetch_tag_types(all_tag_names)

            had_valid = False
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
                if ext not in ["jpg", "jpeg", "png"]:
                    continue

                filename = sanitize_filename(f"{post.get('id')}.{ext}", fallback=f"yande_{post.get('id', 'item')}.jpg")
                rating_label = sanitize_path_component(self.rating_map.get(post_rating, "Unknown"), fallback="Unknown")
                rating_dir = os.path.join(self.tag_dir, rating_label, "images")
                safe_ensure_dir(rating_dir)
                filepath = os.path.join(rating_dir, filename)

                tags_raw = post.get("tags", "")
                tag_names = [t.strip() for t in tags_raw.split() if t.strip()]
                cats = self._categorize_tags(tag_names, cache)
                artists = cats["artist"]
                characters = cats["character"]
                copyrights = cats["copyright"]
                metadata_tags = cats["metadata"]
                tags_list = cats["tag"]

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

def worker_yande(tag, amount, rating, net_config):
    worker = YandeWorker(tag, amount, rating, net_config)
    worker.run()
