import os
import asyncio
from workers import BaseWorker, sanitize_path_component, sanitize_filename, safe_ensure_dir


SORTINGS = {"date_added", "relevance", "random", "views", "favorites", "toplist"}
PURITY_ORDER = ("sfw", "sketchy", "nsfw")
CATEGORY_ORDER = ("general", "anime", "people")
PURITY_DIRS = {"sfw": "Safe", "sketchy": "Questionable", "nsfw": "NSFW"}


def purity_bits(purity):
    sel = {p for p in (purity or "").lower().split()}
    if not sel:
        return "111"  # dropdown "All" collapses to empty
    return "".join("1" if p in sel else "0" for p in PURITY_ORDER)


def category_bits(categories):
    sel = {c for c in (categories or "").lower().split()}
    if not sel:
        return "111"
    return "".join("1" if c in sel else "0" for c in CATEGORY_ORDER)


class WallhavenWorker(BaseWorker):
    def __init__(self, tag, amount, purity, categories, sorting, order, net_config):
        super().__init__("wallhaven", "Wallhaven", amount, net_config)
        self.original_tag = tag.strip()
        self.purity_bits = purity_bits(purity)
        self.category_bits = category_bits(categories)
        self.sorting = sorting if sorting in SORTINGS else "date_added"
        self.order = order if order in ("desc", "asc") else "desc"
        self.retry_wait = int(net_config.get("retry_wait", 5))
        self.apikey = (net_config.get("wallhaven_apikey") or "").strip()

        clean_tag = " ".join(
            t for t in self.original_tag.split() if t and t[0] not in "+-@"
        )
        self.safe_tag = sanitize_path_component(clean_tag, fallback="wallhaven")
        self.tag_dir = os.path.join(self.site_root, self.safe_tag)
        safe_ensure_dir(self.tag_dir)
        self._seed = None
        self._rate_retries = 0

    def get_tags(self):
        return [self.original_tag]

    async def download_image(self, url, filepath, filename, tags_list, artists=None):
        return await self.enqueue_download(url, filepath, filename, tags_list, artists or [])

    async def fetch_posts(self):
        await self.scraper_task()

    async def scraper_task(self):
        self.log(f"Initializing worker for tag: '{self.original_tag or 'latest'}'")

        collected_count = 0
        page = 1
        while not self.stop_event.is_set() and (self.amount == 0 or collected_count < self.amount):
            params = {
                "categories": self.category_bits,
                "purity": self.purity_bits,
                "sorting": self.sorting,
                "order": self.order,
                "page": page,
            }
            if self.original_tag:
                params["q"] = self.original_tag
            if self.apikey:
                params["apikey"] = self.apikey
            if self.sorting == "random" and self._seed:
                params["seed"] = self._seed

            try:
                self.log(f"Scanning API... (Page {page})")
                resp = await self.session.get("https://wallhaven.cc/api/v1/search", params=params)
                if resp.status == 429:
                    # 45 req/min cap — one clean minute clears it
                    self._rate_retries += 1
                    if self._rate_retries > 3:
                        self.log("Rate limited (429) repeatedly — stopping.")
                        break
                    self.log("Rate limited (429). Waiting 60s...")
                    await asyncio.sleep(60)
                    continue
                if resp.status == 401:
                    self.log("ERROR 401: NSFW/API requests need a valid API key — set it in Settings → API Keys.")
                    break
                resp.raise_for_status()
                data = await resp.json()
            except Exception as e:
                self.log(f"API Error: {e}. Retrying in {self.retry_wait}s...")
                await asyncio.sleep(self.retry_wait)
                continue

            items = data.get("data") or []
            meta = data.get("meta") or {}
            if page == 1 and meta.get("seed"):
                self._seed = meta["seed"]
            if not items:
                if page == 1:
                    self.log(f"ZERO images found for '{self.original_tag or 'latest'}'.")
                break

            had_valid = False
            for item in items:
                if self.stop_event.is_set() or (self.amount > 0 and collected_count >= self.amount):
                    break
                if not isinstance(item, dict):
                    continue
                url = item.get("path")
                if not url:
                    continue
                ftype = str(item.get("file_type", ""))
                if ftype and not ftype.startswith("image/"):
                    continue  # ponytail: videos exist on wallhaven, gallery is image-first
                ext = os.path.splitext(url.split("?")[0])[1].lower().lstrip(".") or "jpg"
                item_id = item.get("id", "item")
                filename = sanitize_filename(
                    f"wallhaven-{item_id}.{ext}", fallback=f"wallhaven_{item_id}.jpg"
                )
                subdir = PURITY_DIRS.get(str(item.get("purity", "sfw")).lower(), "Safe")
                rating_dir = os.path.join(self.tag_dir, subdir, "images")
                safe_ensure_dir(rating_dir)
                filepath = os.path.join(rating_dir, filename)
                tags_list = [
                    t for t in self.original_tag.split() if t and t[0] not in "+-@"
                ]
                if await self.enqueue_download(url, filepath, filename, tags_list, []):
                    collected_count += 1
                    had_valid = True

            page += 1
            last_page = meta.get("last_page")
            if (last_page and page > last_page) or page > 500:
                break
            if had_valid:
                await asyncio.sleep(self.anti_ban_pause)

        actual = self.enqueued_count
        if self.stop_event.is_set():
            return
        if actual == 0:
            self.log("No new images to download.")
        else:
            self.check_amount_warning(actual)

    def run(self):
        asyncio.run(self.run_async_loop(self.scraper_task))


def worker_wallhaven(tag, amount, purity, categories, sorting, order, net_config):
    worker = WallhavenWorker(tag, amount, purity, categories, sorting, order, net_config)
    worker.run()
