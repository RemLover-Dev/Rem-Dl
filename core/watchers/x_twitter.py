"""X (Twitter) account watcher: `from:<handle> filter:media` via twscrape.

Search pagination is an opaque cursor (no offset), so this adapter walks
the whole new window in one call and exposes fetch_pages instead of fetch;
the engine stores no page cursor for it. The window starts at the
checkpoint tweet's snowflake timestamp, so a failed check never skips posts.
"""

import asyncio
import time

from workers.x_twitter import TWSCRAPE_DB, XWorker

LIMIT = 20  # engine cap math only; fetch_pages returns the whole window
NAME = "X (Twitter)"
OVERLAP = 120  # seconds of slack on since_time; dedup drops the repeats
SNOWFLAKE_EPOCH_MS = 1288834974657


def query(tags, ratings):
    handle = XWorker.normalize_handle((tags or [""])[0])
    if not handle:
        raise RuntimeError("enter an account handle (e.g. @artist)")
    return f"from:{handle} filter:media -filter:retweets"


def _since(w):
    """Start of the new window: the checkpoint tweet's own timestamp (X ids
    are snowflakes). Falls back to the baseline for a first check."""
    try:
        cid = int(w.get("last_seen_post_id") or 0)
    except (TypeError, ValueError):
        cid = 0
    if cid > 0:
        return max(0, ((cid >> 22) + SNOWFLAKE_EPOCH_MS) // 1000 - OVERLAP)
    base = float(w.get("baseline_at") or w.get("last_checked_at") or 0)
    return int(base - OVERLAP) if base > 0 else int(time.time()) - 86400


def _post(t):
    photos = t.media.photos if t.media else []
    return {
        "id": str(t.id),
        "created_at": t.date.timestamp() if t.date else 0,
        "thumb": photos[0].url if photos else "",
        "photos": [p.url for p in photos],
    }


async def _walk(q, handle, since):
    try:
        from twscrape import API, AccountsPool
    except ImportError:
        raise RuntimeError("twscrape is not installed — run: pip install twscrape")
    from core.database import get_settings
    st = get_settings()
    cfg = st.load_api_settings()
    cookies = XWorker.load_cookie_string(cfg.get("x_cookies") or "")
    username = (cfg.get("x_username") or "").strip()
    if not cookies or not username:
        raise RuntimeError("set X cookies + username in Settings")
    pool = AccountsPool(db_file=TWSCRAPE_DB)
    await pool.add_account_cookies(username, cookies)
    proxy = st.get("proxy_url") if st.get("use_proxy") else None
    api = API(pool=pool, proxy=proxy or None)
    if not any(x["logged_in"] for x in await pool.accounts_info()):
        raise RuntimeError("no account could log in — fix cookies in Settings")
    out = []
    async for t in api.search(f"{q} since_time:{since}", limit=-1):
        if t.retweetedTweet is not None:
            continue
        if (t.user.username or "").lower() != handle.lower():
            continue
        out.append(t)
    return out


def fetch_pages(api_tag, w):
    handle = XWorker.normalize_handle((w.get("tags") or [""])[0])
    tweets = asyncio.run(_walk(api_tag, handle, _since(w)))
    return [_post(t) for t in tweets]


def fields(p):
    thumb = p.get("thumb") or ""
    if thumb and "?" not in thumb:
        thumb += "?name=360x360"
    url = (p.get("photos") or [""])[0]
    if url:
        url = XWorker.orig_photo_url(url)[1]
    return thumb, "", url
