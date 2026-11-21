"""Rule34 watcher adapter: rule34Py library search. Rating tokens have
no API meaning here — the watcher panel hides the rating dropdown; the
library's exclude_ai filter is not exposed."""

from core.watchers.net import api_settings, std_fields

LIMIT = 1000  # dapi max
NAME = "Rule34"


def query(tags, ratings):
    # AND space-join; the dapi honors -tag negations
    return [" ".join(str(t) for t in tags if str(t).strip())]


def fetch(api_tag, pid):
    from rule34Py import rule34Py
    client = rule34Py()
    cfg = api_settings()
    key = cfg.get("rule34_api_key") or ""
    uid = cfg.get("rule34_user_id") or ""
    if key and uid.isdigit():
        client.api_key = key
        client.user_id = int(uid)
    results = client.search(api_tag, page_id=pid, limit=LIMIT) or []
    # rule34Py Post objects -> plain dicts for the generic engine;
    # 'change' (last-modified epoch) stands in for created_at
    return [{"id": p.id, "preview_url": p.thumbnail,
             "sample_url": p.sample or p.image,
             "file_url": p.image, "created_at": p.change} for p in results]


fields = std_fields
