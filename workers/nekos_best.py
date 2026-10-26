import asyncio
import os

from workers import BaseWorker, folder_component, safe_ensure_dir, sanitize_filename


class NekosBestWorker(BaseWorker):
    def __init__(self, category, amount, net_config):
        super().__init__("neko", "Nekos.best", amount, net_config)
        # the dropdown sends a space-joined list; single strings still work
        self.categories = [c for c in str(category or "").replace(",", " ").split() if c] or ["neko"]
        safe_cat = folder_component(self.categories[0], fallback="neko")
        self.cat_dir = os.path.join(self.site_root, safe_cat)
        safe_ensure_dir(self.cat_dir)

    async def scraper_task(self):
        self.log(f"Initializing worker for categories: {', '.join(self.categories)}")

        collected_count = 0
        max_retries = 3
        consecutive_failures = 0
        cats = list(self.categories)
        idx = 0

        while not self.stop_event.is_set() and cats and (self.amount == 0 or collected_count < self.amount):
            # round-robin: spread batches across the selected categories
            cat = cats[idx % len(cats)]
            idx += 1
            batch_size = min(20, self.amount - collected_count if self.amount > 0 else 20)
            fetch_url = f"https://nekos.best/api/v2/{cat}?amount={batch_size}"

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
                    self.log(f"End of database reached for '{cat}'.")
                    cats.remove(cat)
                    if cats:
                        idx = 0
                    continue
            except Exception as e:
                consecutive_failures += 1
                self.log(f"Network delay, retrying... ({consecutive_failures}/{max_retries}) ({e})")
                if consecutive_failures >= max_retries:
                    self.log(f"API unreachable after {max_retries} attempts. Skipping.")
                    break
                await asyncio.sleep(5)
                continue

            cat_dir = os.path.join(self.site_root, folder_component(cat, fallback="neko"))
            safe_ensure_dir(cat_dir)
            for item in results:
                if self.stop_event.is_set() or (self.amount > 0 and collected_count >= self.amount):
                    break
                url = item.get("url")
                if not url:
                    continue
                raw_filename = url.split('/')[-1]
                filename = sanitize_filename(raw_filename, fallback="neko.jpg")
                filepath = os.path.join(cat_dir, filename)
                # image categories carry artist_name; GIF categories only anime_name
                artist_name = item.get("artist_name")
                artists = [artist_name] if artist_name else []
                # anime_name is the series title — copyright bucket, GIFs only
                anime_name = item.get("anime_name")
                copyrights = [anime_name] if anime_name else []
                if await self.enqueue_download(url, filepath, filename, [cat],
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
