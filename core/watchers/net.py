"""Shared plumbing for watcher source adapters. Engine imports adapters;
adapters must never import back (no circulars) — engine bits they need
come through here."""

import requests

from core.database import get_settings

RATING_TOKENS = {"rating:g", "rating:s", "rating:q", "rating:e"}


def get(url, **kw):
    kw.setdefault("timeout", 30)
    kw.setdefault("verify", bool(get_settings().get("verify_tls", True)))
    return requests.get(url, **kw)


def post(url, **kw):
    kw.setdefault("timeout", 30)
    kw.setdefault("verify", bool(get_settings().get("verify_tls", True)))
    return requests.post(url, **kw)


def std_fields(p):
    """dapi / moebooru / sankaku post -> (thumb, rating, url)."""
    return (p.get("preview_url") or p.get("sample_url") or "",
            p.get("rating", ""), p.get("file_url") or "")
