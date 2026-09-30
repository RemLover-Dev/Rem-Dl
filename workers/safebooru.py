import html, os
import asyncio
import xml.etree.ElementTree as ET
from workers import BaseWorker, sanitize_path_component, sanitize_filename, safe_ensure_dir
import core.shared as shared
from core.database import DatabaseManager, DATABASE_DIR

TAG_TYPES_FILE = os.path.join(DATABASE_DIR, "safebooru_tag_types.json")


class SafebooruWorker(BaseWorker):
    def __init__(self, tag, amount, exclusions, net_config):
        super().__init__("safe", "Safebooru", amount, net_config)
        self.original_tag = tag.strip().lower()
        self.exclusions = exclusions

        clean_tag = " ".join(t for t in self.original_tag.split() if not t.startswith('-'))
        self.safe_tag = sanitize_path_component(clean_tag, fallback="safebooru")
        self.tag_dir = os.path.join(self.site_root, self.safe_tag)
        safe_ensure_dir(self.tag_dir)
        # persistent across runs — known tags never refetch (verified types only)
        cached = DatabaseManager.load_json(TAG_TYPES_FILE)
        self.tag_cache = dict(cached) if isinstance(cached, dict) else {}

    async def _fetch_tag_types(self, tag_names):
        cache = self.tag_cache
        # ponytail: cap per run — uncached tags page through on later calls
        # instead of stalling the first run
        uncached = [t for t in tag_names if t not in cache][:150]
        if uncached:
            # ponytail: one request per tag SEQUENTIALLY stalled every page
            # for 10-25s — same concurrent pattern as the gelbooru worker
            sem = asyncio.Semaphore(4)
            async def query_one(tag_name):
                async with sem:
                    # ponytail: 3 attempts — a transient failure must not leave
                    # a tag miscategorized for this whole run
                    for attempt in range(3):
                        fetched = False
                        try:
                            resp = await self.session.get("https://safebooru.org/index.php", params={
                                "page": "dapi", "s": "tag", "q": "index",
                                # safebooru tag search needs entity-encoded names (kal&#039;tsit_...); &#x27; doesn't match
                                "name": html.escape(tag_name, quote=False).replace("'", "&#039;"), "limit": 50
                            })
                            if resp.status == 200:
                                text = await resp.text()
                                root = ET.fromstring(text)
                                # ponytail: scan every row for the exact tag
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
        self.log(f"Initializing worker for tag: '{self.original_tag}'")

        collected_count = 0
        pid = 0

        consecutive_errors = 0
        while not self.stop_event.is_set() and (self.amount == 0 or collected_count < self.amount):
            try:
                self.log(f"Scanning API... (Page {pid})")
                limit_val = min(100, self.amount - collected_count if self.amount > 0 else 100)

                resp = await self.session.get("https://safebooru.org/index.php", params={
                    "page": "dapi", "s": "post", "q": "index",
                    "tags": self.original_tag, "pid": pid, "limit": limit_val, "json": 1
                })
                if resp.status in [403, 429]:
                    self.log(f"ERROR {resp.status}. Change proxy.")
                    break
                resp.raise_for_status()

                data = await resp.json()
                if isinstance(data, dict):
                    posts = data.get("post", [])
                elif isinstance(data, list):
                    posts = data
                else:
                    posts = []
                if not posts:
                    if pid == 0:
                        self.log(f"ZERO images found for '{self.original_tag}'.")
                    else:
                        self.log("End of database reached.")
                    break

            except Exception as e:
                err_str = str(e)
                if "403" in err_str:
                    self.log("ERROR 403: Cloudflare/ISP block. You need a VPN.")
                else:
                    self.log(f"API Error: {e}")
                consecutive_errors += 1
                if consecutive_errors >= 3:
                    self.log("API failed 3 times in a row — giving up.")
                    break
                await asyncio.sleep(5)
                continue

            consecutive_errors = 0
            all_tag_names = set()
            for post in posts:
                if not isinstance(post, dict):
                    continue
                tags_raw = post.get("tag_string", post.get("tags", ""))
                # post tags arrive entity-encoded (kal&#039;tsit_...) — normalize before cache lookup
                all_tag_names.update(html.unescape(t.strip()) for t in tags_raw.split() if t.strip())

            cache = await self._fetch_tag_types(all_tag_names)

            had_valid = False
            for post in posts:
                if self.stop_event.is_set() or (self.amount > 0 and collected_count >= self.amount):
                    break
                if not isinstance(post, dict):
                    continue

                file_url = post.get("file_url") or post.get("large_file_url")
                if not file_url:
                    continue

                ext = file_url.split('.')[-1].lower().split('?')[0]
                if ext in ["mp4", "webm", "zip"] and "-video" in self.exclusions:
                    continue
                if ext in ["jpg", "jpeg", "png", "webp"] and "-image" in self.exclusions:
                    continue
                if ext == "gif" and "-gif" in self.exclusions:
                    continue

                filename = sanitize_filename(f"{post.get('id')}.{ext}", fallback=f"safebooru_{post.get('id', 'item')}.jpg")
                safe_dir = os.path.join(self.tag_dir, "Safe", "images")
                safe_ensure_dir(safe_dir)
                filepath = os.path.join(safe_dir, filename)

                tags_raw = post.get("tag_string", post.get("tags", ""))
                tag_names = [html.unescape(t.strip()) for t in tags_raw.split() if t.strip()]
                cats = self._categorize_tags(tag_names, cache)
                artists = cats["artist"]
                characters = cats["character"]
                copyrights = cats["copyright"]
                metadata_tags = cats["metadata"]
                tags_list = cats["tag"]

                if await self.enqueue_download(file_url, filepath, filename, tags_list, artists, characters, copyrights, metadata_tags):
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

    def run(self):
        asyncio.run(self.run_async_loop(self.scraper_task))

def worker_safebooru(tag, amount, exclusions, net_config):
    worker = SafebooruWorker(tag, amount, exclusions, net_config)
    worker.run()
