import os
import asyncio
from core.shared import BaseDownloader, sanitize_path_component, sanitize_filename, safe_ensure_dir

class NekosApiWorker(BaseDownloader):
    def __init__(self, tags, amount, rating, net_config):
        super().__init__("nekosapi", "NekosAPI", amount, net_config)
        self.tags = [t.strip() for t in tags.split(",") if t.strip()]
        self.exclusions = []
        for t in self.tags[:]:
            if t.startswith("-"):
                self.exclusions.append(t[1:])
                self.tags.remove(t)
        if not self.tags:
            self.tags = ["kemonomimi"]
        self.rating_map = {"safe": "Safe", "suggestive": "Sensitive",
                           "borderline": "Questionable", "explicit": "NSFW"}
        codes = []
        for tok in (rating or "").split():
            tok = tok.strip().lower().removeprefix("rating:")
            if tok in self.rating_map and tok not in codes:
                codes.append(tok)
        self.rating_codes = codes
        self.rating_allowed = set(codes)
        # nekosapi natively ORs comma lists in the rating param (verified live)
        # — full set and "All Ratings" both mean no server-side filter
        self.api_rating = ",".join(codes) if len(codes) < len(self.rating_map) else ""
        self.rating_display = ", ".join(self.rating_map[c] for c in codes)

        self.api_base = "https://api.nekosapi.com/v4"
        safe_tag_folder = sanitize_path_component("_".join(self.tags), fallback="kemonomimi")
        self.tag_dir = os.path.join(self.site_root, safe_tag_folder)
        folder_label = "_".join(self.rating_map[c] for c in codes) or "All Ratings"
        safe_rating_label = sanitize_path_component(folder_label, fallback="Safe")
        self.rating_dir = os.path.join(self.tag_dir, safe_rating_label)
        safe_ensure_dir(self.rating_dir)

    def _query_params(self, limit, offset):
        params = {"limit": limit, "offset": offset}
        if self.tags: params["tags"] = ",".join(self.tags)
        if self.exclusions: params["without_tags"] = ",".join(self.exclusions)
        if self.api_rating: params["rating"] = self.api_rating
        return params

    async def scraper_task(self):
        self.log(f"Initializing worker for tags: {self.tags}")
        self.log(f"Rating: {self.rating_display or 'All Ratings'}")

        need = self.amount or 200
        collected = 0
        offset = 0
        batch_size = 50

        while collected < need and not self.stop_event.is_set():
            params = self._query_params(min(batch_size, need - collected), offset)

            try:
                async with self.session.get(f"{self.api_base}/images", params=params) as resp:
                    if resp.status in (403, 429):
                        self.log(f"API BAN ({resp.status}). Change VPN node.")
                        break
                    resp.raise_for_status()
                    data = await resp.json()

                images = data.get("items", [])
                if not images:
                    self.log("No new images to download.")
                    break
            except Exception as e:
                self.log(f"API error: {e}")
                break

            for img in images:
                if self.stop_event.is_set() or collected >= need: break
                # safety net for anything the server-side rating filter let through
                if self.rating_allowed and img.get("rating") not in self.rating_allowed:
                    continue
                url = img.get("url")
                if not url: continue
                img_id = img.get("id", "unknown")
                ext = url.rsplit(".", 1)[-1].split("?")[0]
                if ext.lower() not in {"jpg", "jpeg", "png", "webp", "gif", "bmp", "tiff"}:
                    ext = "jpg"
                filename = sanitize_filename(f"{img_id}.{ext}", fallback=f"nekosapi_{img_id}.jpg")
                filepath = os.path.join(self.rating_dir, filename)
                tag_list = self.tags + [t for t in img.get("tags", []) if t]
                artist_name = img.get("artist_name")
                artists = [artist_name] if artist_name else []
                if await self.enqueue_download(url, filepath, filename, tag_list, artists):
                    collected += 1

            if collected >= need or not images: break
            offset += len(images)
            if not self.stop_event.is_set():
                await asyncio.sleep(self.anti_ban_pause)

        # ponytail: stopped runs wind down late — never paint summaries over the next run
        if self.stop_event.is_set():
            return
        if collected:
            self.log(f"Enqueued {collected} item{'s' if collected != 1 else ''}.")


def worker_nekosapi(tags, amount, rating, net_config):
    NekosApiWorker(tags, amount, rating, net_config).run()
