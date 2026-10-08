import os
import asyncio
from workers import BaseWorker, sanitize_path_component, sanitize_filename, safe_ensure_dir


class NekosBestWorker(BaseWorker):
    def __init__(self, category, amount, net_config):
        super().__init__("neko", "Nekos.best", amount, net_config)
        self.category = category
        safe_cat = sanitize_path_component(category, fallback="neko")
        self.cat_dir = os.path.join(self.site_root, safe_cat)
        safe_ensure_dir(self.cat_dir)

    async def scraper_task(self):
        self.log(f"Initializing worker for category: '{self.category}'")

        collected_count = 0
        max_retries = 3
        consecutive_failures = 0

        while not self.stop_event.is_set() and (self.amount == 0 or collected_count < self.amount):
            batch_size = min(20, self.amount - collected_count if self.amount > 0 else 20)
            fetch_url = f"https://nekos.best/api/v2/{self.category}?amount={batch_size}"

            try:
                self.log("Scanning API...")
                resp = await self.session.get(fetch_url)
                if resp.status in [403, 429]:
                    self.log(f"API BAN ({resp.status}). Change VPN node.")
                    break
                resp.raise_for_status()
                data = await resp.json()
                results = data.get("results", [])
                consecutive_failures = 0
                if not results:
                    self.log("End of database reached.")
                    break
            except Exception as e:
                consecutive_failures += 1
                self.log(f"Network delay, retrying... ({consecutive_failures}/{max_retries}) ({e})")
                if consecutive_failures >= max_retries:
                    self.log(f"API unreachable after {max_retries} attempts. Skipping.")
                    break
                await asyncio.sleep(5)
                continue

            for item in results:
                if self.stop_event.is_set() or (self.amount > 0 and collected_count >= self.amount):
                    break
                url = item.get("url")
                if not url:
                    continue
                raw_filename = url.split('/')[-1]
                filename = sanitize_filename(raw_filename, fallback="neko.jpg")
                filepath = os.path.join(self.cat_dir, filename)
                # image categories carry artist_name; GIF categories only anime_name
                artist_name = item.get("artist_name")
                artists = [artist_name] if artist_name else []
                # anime_name is the series title — copyright bucket, GIFs only
                anime_name = item.get("anime_name")
                copyrights = [anime_name] if anime_name else []
                if await self.enqueue_download(url, filepath, filename, [self.category],
                                               artists, copyrights=copyrights):
                    collected_count += 1

            if not self.stop_event.is_set() and (self.amount == 0 or collected_count < self.amount):
                await asyncio.sleep(self.anti_ban_pause)

        actual = self.enqueued_count
        # ponytail: stopped runs wind down late — never paint summaries over the next run
        if self.stop_event.is_set():
            return
        if actual == 0:
            self.log("No new images to download.")
        else:
            self.check_amount_warning(actual)


def worker_nekos_best(category, amount, net_config):
    worker = NekosBestWorker(category, amount, net_config)
    worker.run()
