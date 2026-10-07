import asyncio
from abc import ABC, abstractmethod
from core.shared import (
    BaseDownloader,
    sanitize_path_component,
    sanitize_filename,
    safe_ensure_dir,
    safe_filepath,
    media_subdir
)
from core.database import DatabaseManager

__all__ = [
    "BaseWorker",
    "BaseDownloader",
    "sanitize_path_component",
    "sanitize_filename",
    "safe_ensure_dir",
    "safe_filepath",
    "media_subdir"
]


class BaseWorker(BaseDownloader, ABC):
    """Abstract base class for all site-specific scrapers.

    Inherits download-engine capabilities from BaseDownloader and adds
    abstract interface methods that every worker must implement.

    Required overrides:
      - scraper_task()     : async – core scraping logic (inherited from BaseDownloader pattern)

    run() is concrete — every worker shares the same sync entry point.
    Override only if the worker needs setup/teardown (e.g. pinterest proxy env).
    """

    def __init__(self, name, site_folder, amount, net_config):
        super().__init__(name, site_folder, amount, net_config)

    @abstractmethod
    async def scraper_task(self):
        """Core scraping logic. Must be an async coroutine."""
        ...

    @staticmethod
    def clean_tag(tag: str) -> str:
        """Remove filesystem-unsafe characters from a tag string."""
        import re
        cleaned = " ".join(t for t in tag.split() if not t.startswith('-'))
        return re.sub(r'[\\/*?:"<>|]', "", cleaned)

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

    async def _run_tag_fetch(self, tag_names, attempt_one, types_file):
        """Shared tag-fetch skeleton (yande/safebooru): uncached filter
        (150/run) → Semaphore(4) → 3-attempt retry with backoff → gather →
        persist. attempt_one(tag) returns True once a 200 was processed
        (exact match stored or genuinely absent); non-200/exception retries
        and a tag that never comes back stays uncached."""
        uncached = [t for t in tag_names if t not in self.tag_cache][:150]
        if not uncached:
            return self.tag_cache
        # ponytail: concurrent — sequential per-tag requests stalled every page
        sem = asyncio.Semaphore(4)

        async def query_one(tag_name):
            async with sem:
                # ponytail: 3 attempts — a transient failure must not leave
                # a tag miscategorized for this whole run
                for attempt in range(3):
                    fetched = False
                    try:
                        fetched = await attempt_one(tag_name)
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
        await asyncio.to_thread(DatabaseManager.save_json, types_file, self.tag_cache)
        return self.tag_cache
