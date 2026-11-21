"""Shared plumbing for watcher source adapters. Engine imports adapters;
adapters must never import back (no circulars) — engine bits they need
come through here."""

import requests

from core.database import get_settings

RATING_TOKENS = {"rating:g", "rating:s", "rating:q", "rating:e"}

# danbooru's Cloudflare blocks python-requests' default UA (403), and its
# own API asks for an identifying agent — send what the downloader sends
DEFAULT_HEADERS = {
    "User-Agent": "Rems_Dl/5.0 (by RemLover on GitHub)",
    "Accept": "application/json",
}


def api_settings():
    """Current API credentials. Reads .env too, so keys saved from the
    Settings UI are used even when os.environ was never updated."""
    return get_settings().load_api_settings()


def _headers(kw):
    merged = dict(DEFAULT_HEADERS)
    merged.update(kw.pop("headers", None) or {})
    return merged


def get(url, **kw):
    kw.setdefault("timeout", 30)
    kw.setdefault("verify", bool(get_settings().get("verify_tls", True)))
    return requests.get(url, headers=_headers(kw), **kw)


def post(url, **kw):
    kw.setdefault("timeout", 30)
    kw.setdefault("verify", bool(get_settings().get("verify_tls", True)))
    return requests.post(url, headers=_headers(kw), **kw)


def std_fields(p):
    """dapi / moebooru / sankaku post -> (thumb, rating, url)."""
    return (p.get("preview_url") or p.get("sample_url") or "",
            p.get("rating", ""), p.get("file_url") or "")
