"""Zerochan watcher adapter: ?json API over curl_cffi/chrome131.
No created_at in the listing -> the baseline swallows everything."""

import json
import urllib.parse

LIMIT = 200
BASE = "https://www.zerochan.net"


def query(tags, ratings):
    # AND via spaces inside one group; the site search has no negation —
    # dashed tokens are dropped (limitation, not filtered client-side)
    tag = " ".join(str(t) for t in tags
                   if str(t).strip() and not str(t).startswith("-"))
    return urllib.parse.quote_plus(tag.strip())


def fetch(encoded_tag, pid):
    """One page (p is 1-based; pid stays 0-based)."""
    from curl_cffi import requests as curl_requests
    from core.database import get_settings
    st = get_settings()
    s = curl_requests.Session(impersonate="chrome131")
    if st.get("use_proxy") and st.get("proxy_url"):
        s.proxies = {"http": st["proxy_url"], "https": st["proxy_url"]}
    url = f"{BASE}/{encoded_tag}?json=1&l={LIMIT}&p={pid + 1}"
    resp = s.get(url, timeout=30, allow_redirects=True,
                 headers={"Referer": "https://www.zerochan.net/",
                          "Accept": "application/json",
                          # a browser UA gets the "Checking browser..."
                          # interstitial; zerochan allows gallery-dl clients
                          "User-Agent": "gallery-dl"},
                 verify=bool(st.get("verify_tls", True)))
    text = resp.text or ""
    if not text or (resp.status_code != 200 and '"items"' not in text):
        raise RuntimeError(f"zerochan HTTP {resp.status_code}")
    try:
        data = json.loads(text)
    except ValueError:
        # the template sometimes answers 500 with the real object wrapped in
        # junk ("{\r\n{...items...}\r\n</ul>"); re-anchor and stop at its end
        pos = text.find('"items"')
        if pos < 0:
            raise
        data = json.JSONDecoder().raw_decode("{" + text[pos:])[0]
    return data.get("items") or []


def fields(p):
    return (p.get("thumbnail") or p.get("thumb") or "",
            "", p.get("file_url") or p.get("full") or "")
