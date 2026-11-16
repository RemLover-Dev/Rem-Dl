"""anime-pictures.net watcher adapter (api v3). Listing posts carry md5 +
pubtime but no preview URL — the small preview is derivable from the md5
(opreviews.anime-pictures.net/<md5[:3]>/<md5>_sp.avif, the same shape the
post-detail endpoint returns). No per-post rating filter: the API's
`erotics` int is exposed but the site search UI does not filter on it,
so like zerochan/rule34 the source runs without ratings.

The API sits behind Cloudflare ("Just a moment..." for plain requests) —
same curl_cffi/chrome131 transport as the site's own worker and the
zerochan adapter."""

API = "https://api.anime-pictures.net/api/v3"
# the worker uses the same page size — the API caps it silently
LIMIT = 80


def _cf_get(url, **kw):
    from curl_cffi import requests as curl_requests
    from core.database import get_settings
    st = get_settings()
    s = curl_requests.Session(impersonate="chrome131")
    if st.get("use_proxy") and st.get("proxy_url"):
        s.proxies = {"http": st["proxy_url"], "https": st["proxy_url"]}
    s.cookies.set("time_zone", "UTC", domain=".anime-pictures.net")
    s.cookies.set("sitelang", "en", domain=".anime-pictures.net")
    kw.setdefault("timeout", 30)
    kw.setdefault("verify", bool(st.get("verify_tls", True)))
    return s.get(url, **kw)


def query(tags, ratings):
    # AND via spaces; the site search has no '-' negation — dashed
    # tokens are dropped (limitation, not filtered client-side)
    return " ".join(str(t) for t in tags
                    if str(t).strip() and not str(t).startswith("-")).strip()


def fetch(tag, pid):
    """One page (0-based; the API pages from 0)."""
    resp = _cf_get(f"{API}/posts", params={"page": pid, "limit": LIMIT,
                                           "search_tag": tag, "lang": "en"})
    if resp.status_code != 200:
        raise RuntimeError(f"anime_dl HTTP {resp.status_code}")
    return resp.json().get("posts") or []


def fields(p):
    md5 = p.get("md5") or ""
    # _png over _sp.avif: same small preview, but PNG renders in every
    # browser the app can embed (AVIF needs a newer engine)
    thumb = (f"https://opreviews.anime-pictures.net/{md5[:3]}/{md5}_sp.png"
             if md5 else "")
    return (thumb, "", f"https://anime-pictures.net/posts/{p.get('id')}")
