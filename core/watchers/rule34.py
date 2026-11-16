"""Rule34 watcher adapter: rule34Py library search. Rating tokens have
no API meaning here — the watcher panel hides the rating dropdown; the
library's exclude_ai filter is not exposed."""

import os

from core.watchers.net import std_fields

LIMIT = 1000  # dapi max


def query(tags, ratings):
    # AND space-join; the dapi honors -tag negations
    return [" ".join(str(t) for t in tags if str(t).strip())]


def fetch(api_tag, pid):
    from rule34Py import rule34Py
    client = rule34Py()
    key = os.getenv("RULE34_API_KEY", "")
    uid = os.getenv("RULE34_USER_ID", "")
    if key and uid.isdigit():
        client.api_key = key
        client.user_id = int(uid)
    results = client.search(api_tag, page_id=pid, limit=LIMIT) or []
    # rule34Py Post objects -> plain dicts for the generic engine;
    # 'change' (last-modified epoch) stands in for created_at
    return [{"id": p.id, "preview_url": p.preview, "sample_url": p.sample,
             "file_url": p.image, "created_at": p.change} for p in results]


fields = std_fields
