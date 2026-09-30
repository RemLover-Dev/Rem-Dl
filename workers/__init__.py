from abc import ABC, abstractmethod
from core.shared import (
    BaseDownloader,
    sanitize_path_component,
    sanitize_filename,
    safe_ensure_dir,
    safe_filepath
)

__all__ = [
    "BaseWorker",
    "BaseDownloader",
    "sanitize_path_component",
    "sanitize_filename",
    "safe_ensure_dir",
    "safe_filepath"
]


class BaseWorker(BaseDownloader, ABC):
    """Abstract base class for all site-specific scrapers.

    Inherits download-engine capabilities from BaseDownloader and adds
    abstract interface methods that every worker must implement.

    Required overrides:
      - scraper_task()     : async – core scraping logic (inherited from BaseDownloader pattern)
      - run()              : sync  – synchronous entry point
    """

    def __init__(self, name, site_folder, amount, net_config):
        super().__init__(name, site_folder, amount, net_config)

    @abstractmethod
    async def scraper_task(self):
        """Core scraping logic. Must be an async coroutine."""
        ...

    @abstractmethod
    def run(self):
        """Synchronous entry point. Typically calls asyncio.run(...)."""
        ...

    @staticmethod
    def clean_tag(tag: str) -> str:
        """Remove filesystem-unsafe characters from a tag string."""
        import re
        cleaned = " ".join(t for t in tag.split() if not t.startswith('-'))
        return re.sub(r'[\\/*?:"<>|]', "", cleaned)
