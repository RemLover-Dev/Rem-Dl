import os
import asyncio
from workers import BaseWorker, sanitize_path_component, sanitize_filename, safe_ensure_dir
import core.shared as shared


def waifu_name_to_slug(name):
    name_lower = name.lower().strip()
    if name_lower in shared.WAIFU_TAG_MAP:
        return shared.WAIFU_TAG_MAP[name_lower]
    return name_lower.replace(" ", "-")


class WaifuImWorker(BaseWorker):
    def __init__(self, tag, amount, rating, exclusions, net_config):
        super().__init__("waifu", "Waifu.im", amount, net_config)
        self.original_tag = (tag or "").strip().lower()
        self.rating = (rating or "").strip().lower()

        parts = self.original_tag.split()
        include = [t for t in parts if not t.startswith("-")]
        exclude = [t[1:] for t in parts if t.startswith("-") and t[1:]]
        exclude += [str(t).strip("-").lower() for t in (exclusions or []) if str(t).strip("-")]
        self.include_slugs = list(dict.fromkeys(waifu_name_to_slug(t) for t in include))
        self.exclude_slugs = list(dict.fromkeys(waifu_name_to_slug(t) for t in exclude))

        self.api_timeout = int(net_config.get("api_timeout", 10))
        self.retry_wait = int(net_config.get("retry_wait", 5))

        clean_tag = " ".join(include)
        self.safe_tag = sanitize_path_component(clean_tag, fallback="waifu")
        self.tag_dir = os.path.join(self.site_root, self.safe_tag)
        for sub in ("Safe", "NSFW"):
            safe_ensure_dir(os.path.join(self.tag_dir, sub))

    # docs: False = SFW only, True = NSFW only, All = both;
    # the UI's "All Ratings" (empty value) means both
    def is_nsfw_param(self):
        vals = set(self.rating.split())
        if "safe" in vals and "nsfw" in vals:
            return "All"
        if "nsfw" in vals:
            return "True"
        if "safe" in vals:
            return "False"
        return "All"

    def page_params(self, page):
        params = {"pageSize": 50, "page": page, "IsNsfw": self.is_nsfw_param()}
        if self.include_slugs:
            params["IncludedTags"] = self.include_slugs
        if self.exclude_slugs:
            params["ExcludedTags"] = self.exclude_slugs
        return params

    async def scraper_task(self):
        self.log(f"Initializing worker for tags: '{self.original_tag}' -> slugs {self.include_slugs} (IsNsfw: {self.is_nsfw_param()})")

        collected_count = 0
        page = 1

        consecutive_errors = 0
        while not self.stop_event.is_set() and (self.amount == 0 or collected_count < self.amount):
            # ponytail: API shuffles per request, so tags > pageSize can dupe/miss across pages; dedupe keeps it correct, bump pageSize if big tags matter
            params = self.page_params(page)

            try:
                self.log(f"Scanning API... (Page {page})")
                resp = await self.session.get("https://api.waifu.im/images", params=params, headers={"User-Agent": "Mozilla/5.0"})
                if resp.status == 404:
                    self.log("ERROR 404: Tag not found!")
                    break
                elif resp.status == 403:
                    self.log("ERROR 403: Adult tag detected. Set Rating to NSFW or All.")
                    break
                resp.raise_for_status()

                data = await resp.json()
                items = data.get("items", [])
                total = data.get("totalCount", 0)

                if not items or total == 0:
                    self.log(f"No images found for '{self.original_tag}'.")
                    break
            except Exception as e:
                self.log(f"API Error: {e}. Retrying in {self.retry_wait}s...")
                consecutive_errors += 1
                if consecutive_errors >= 3:
                    self.log("API failed 3 times in a row — giving up.")
                    break
                await asyncio.sleep(self.retry_wait)
                continue

            consecutive_errors = 0
            for img in items:
                if self.stop_event.is_set() or (self.amount > 0 and collected_count >= self.amount):
                    break
                url = img.get("url")
                if not url:
                    continue

                raw_filename = url.split('/')[-1]
                filename = sanitize_filename(raw_filename, fallback=f"waifu_{img.get('id', 'item')}.jpg")
                subdir = "NSFW" if img.get("isNsfw") else "Safe"
                filepath = os.path.join(self.tag_dir, subdir, filename)

                tags = [t.get("slug", "").replace("-", "_") for t in img.get("tags", []) if t.get("slug")]
                for t in self.original_tag.split():
                    if not t.startswith("-") and t not in tags:
                        tags.append(t)
                # ponytail: no rating:* pseudo-tags — rating comes from the Safe/NSFW subdir
                artists = [a.get("name", "") for a in img.get("artists", []) if a.get("name")]

                if await self.enqueue_download(url, filepath, filename, tags, artists):
                    collected_count += 1

            page += 1
            has_next = data.get("hasNextPage", False)
            if not has_next or self.stop_event.is_set() or (self.amount > 0 and collected_count >= self.amount):
                break
            await asyncio.sleep(self.anti_ban_pause)

        actual = self.enqueued_count
        # ponytail: stopped runs wind down late — never paint summaries over the next run
        if self.stop_event.is_set():
            return
        if actual == 0:
            self.log("No new images to download.")
        else:
            self.check_amount_warning(actual)


def worker_waifu(tag, amount, rating, exclusions, net_config):
    worker = WaifuImWorker(tag, amount, rating, exclusions, net_config)
    worker.run()
