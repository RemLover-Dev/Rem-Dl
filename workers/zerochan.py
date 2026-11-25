import asyncio
import json as _json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from html.parser import HTMLParser
from typing import Any

import urllib3
import xml.etree.ElementTree as ET
from curl_cffi import requests as curl_requests

from core import shared
from core.dedup_store import hash_bytes
from core.shared import (
    BaseDownloader,
    absorb_duplicate,
    add_to_gallery,
    build_tagd,
    check_duplicate,
    dims_log_token,
    folder_component,
    pace_session,
    safe_ensure_dir,
    sanitize_filename,
    save_history,
    send_tags,
    write_image_metadata,
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class _ZerochanSubtagParser(HTMLParser):
    """Parse sub-tag browser boxes from a Zerochan tag page.

    Targets ``<section class="carousel thumbs"><ul><li>`` entries:
        <li><a href="/Reze+%28Default+Outfit%29" title="235 entries">
        <div class="thumb" data-src="..."></div>
        <p class="outfit">Default Outfit</p></a><i>235</i></li>
    """

    def __init__(self):
        super().__init__()
        self.subtags = []
        self._in_carousel = False
        self._current = None
        self._in_name = False
        self._in_count = False

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag == "section" and "carousel" in (attrs_dict.get("class") or "").split():
            self._in_carousel = True
            return
        if not self._in_carousel:
            return
        if tag == "li":
            self._current = {"name": "", "short": "", "url": "",
                             "count": 0, "thumb": "", "kind": ""}
        elif tag == "a" and self._current is not None and not self._current["url"]:
            href = attrs_dict.get("href") or ""
            if href.startswith("/"):
                self._current["url"] = "https://www.zerochan.net" + href
                self._current["name"] = urllib.parse.unquote_plus(
                    href.rsplit("/", 1)[-1])
            title = attrs_dict.get("title") or ""
            m = re.search(r"(\d+)", title)
            if m:
                self._current["count"] = int(m.group(1))
        elif tag == "div" and self._current is not None:
            if "thumb" in (attrs_dict.get("class") or "").split():
                self._current["thumb"] = attrs_dict.get("data-src", "")
        elif tag == "p" and self._current is not None:
            self._current["kind"] = attrs_dict.get("class", "")
            self._in_name = True
        elif tag == "i" and self._current is not None:
            self._in_count = True

    def handle_data(self, data):
        if self._current is None:
            return
        if self._in_name:
            self._current["short"] += data.strip()
        elif self._in_count:
            m = re.search(r"(\d+)", data)
            if m:
                self._current["count"] = int(m.group(1))

    def handle_endtag(self, tag):
        if tag == "section" and self._in_carousel:
            self._in_carousel = False
        if not self._in_carousel and tag != "li":
            self._in_name = self._in_count = False
            return
        if tag == "p":
            self._in_name = False
        elif tag == "i":
            self._in_count = False
        elif tag == "li" and self._current is not None:
            if self._current["url"]:
                self.subtags.append(self._current)
            self._current = None
            self._in_name = self._in_count = False


def parse_zerochan_subtags(html):
    """Extract sub-tag boxes from raw Zerochan tag-page HTML.

    Returns a list of dicts:
        [{"name": "Reze (Default Outfit)", "short": "Default Outfit",
          "url": "https://www.zerochan.net/Reze+%28Default+Outfit%29",
          "count": 235, "thumb": "https://...", "kind": "outfit"}, ...]
    """
    parser = _ZerochanSubtagParser()
    try:
        parser.feed(html)
    except Exception:
        pass
    return parser.subtags


class _ZerochanTagParser(HTMLParser):
    """Parse categorized tags from Zerochan post HTML."""

    def __init__(self):
        super().__init__()
        self.tags = []
        self._in_tag_list = False
        self._current_tag = None

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)

        if tag == "ul" and attrs_dict.get("id") == "tags":
            self._in_tag_list = True
            return

        if self._in_tag_list and tag == "li":
            classes = (attrs_dict.get("class") or "").split()
            data_tag = attrs_dict.get("data-tag", "")

            if data_tag and classes:
                category = classes[0]
                primary = "primary" in classes
                favorite = "fav" in classes
                self._current_tag = {
                    "tag": data_tag,
                    "category": category,
                    "primary": primary,
                    "favorite": favorite,
                }

    def handle_endtag(self, tag):
        if tag == "ul" and self._in_tag_list:
            self._in_tag_list = False
        if tag == "li" and self._current_tag is not None:
            self.tags.append(self._current_tag)
            self._current_tag = None


def parse_zerochan_tags(html):
    """Extract categorized tags from raw Zerochan post HTML.

    Returns a list of dicts:
        [{"tag": "Mavuika", "category": "character", "primary": True, "favorite": False}, ...]

    The category comes from the actual CSS class on the <li>, so new
    categories added by Zerochan are handled automatically.
    """
    parser = _ZerochanTagParser()
    try:
        parser.feed(html)
    except Exception:
        pass
    return parser.tags


def _derive_img_url(item):
    # "source" is deliberately absent: ?json returns a pixiv *page* URL there,
    # not an image. id-only items fall through to None so the caller probes
    # static.zerochan.net for the real extension (blind .full.N.jpg is wrong
    # for png/webp posts).
    keys = ["full", "large", "file_url", "src", "url", "image"]
    for k in keys:
        v = item.get(k)
        if v:
            return v
    thumb = item.get("thumb", "")
    if thumb:
        no_thumb = thumb.replace(".thumb.", ".")
        if no_thumb != thumb:
            return no_thumb
    return None


_CF_MARKERS = ("just a moment", "cf-challenge", "attention required",
               "checking your browser", "checking browser", "cf_chl")


# Windows forbids the characters below (plus control chars) in file/dir
# names. NOTE: ':' must be included — Zerochan tags/filenames contain it
# (e.g. "Rem (Re:Zero)", "Uma.Musume:.Pretty.Derby.full.x.png") and it
# crashes os.makedirs/os.replace with WinError 267/87.
_UNSAFE_PATH_CHARS_RE = re.compile('[<>:"/\\\\|?*\\x00-\\x1f]')


def _looks_like_cf_challenge(status_code, text):
    if status_code in (403, 503):
        lowered = (text or "")[:4000].lower()
        return any(m in lowered for m in _CF_MARKERS)
    return False


class _DummyAsyncSession:
    """Minimal stand-in so BaseDownloader.run_async_loop teardown is a no-op."""
    closed = True

    async def close(self):
        return None


class ZerochanWorker(BaseDownloader):
    """Zerochan downloader built entirely on curl_cffi (no gallery-dl).

    Enumeration uses Zerochan's own ``?json`` listing (XML feed as fallback);
    one post-page GET then supplies the categorized tags and the exact
    full-image URL. File downloads always use curl_cffi.

    NOTE: intentionally does NOT use aiohttp anywhere in this worker —
    Cloudflare's "Under Attack" mode fingerprints TLS/JA3, which aiohttp
    cannot impersonate.
    """

    def __init__(self, tag, amount, net_config):
        super().__init__("zero", "Zerochan", amount, net_config)
        self.original_tag = (tag or "").strip()
        self.original_tag_lower = self.original_tag.lower()

        clean_tag = " ".join(t for t in self.original_tag_lower.split() if not t.startswith('-'))
        self.safe_tag = folder_component(clean_tag, fallback="misc")
        self.tag_dir = os.path.join(self.site_root, self.safe_tag)
        try:
            os.makedirs(self.tag_dir, exist_ok=True)
        except OSError:
            # Paranoia: never let a weird tag kill the worker thread silently.
            self.safe_tag = "misc"
            self.tag_dir = os.path.join(self.site_root, self.safe_tag)
            os.makedirs(self.tag_dir, exist_ok=True)
        tag_parts = [urllib.parse.quote_plus(p.strip()) for p in self.original_tag.split(',') if p.strip()]
        self.encoded_tag = ','.join(tag_parts) or urllib.parse.quote_plus(self.original_tag)

        self._zerochan_user = (net_config.get("zerochan_login", "") or "").strip()
        self._zerochan_pass = (net_config.get("zerochan_password", "") or "").strip()
        # Final fallback: server .env (covers stale page / direct socket calls).
        if not self._zerochan_user:
            self._zerochan_user = (os.getenv("ZEROCHAN_LOGIN", "")
                                   or os.getenv("ZEROCHAN_USERNAME", "")).strip()
        if not self._zerochan_pass:
            self._zerochan_pass = os.getenv("ZEROCHAN_PASSWORD", "").strip()

        self.curl_session = None
        self.proxy = None

        if not (self._zerochan_user and self._zerochan_pass):
            self.log("For more access, please set your Zerochan Login in the Settings tab.")


    # --- session factory (overrides aiohttp) ---
    def _ensure_curl_session(self):
        if self.curl_session is not None:
            return self.curl_session
        s = curl_requests.Session(impersonate="chrome131")
        if self.net_config.get("use_proxy"):
            p = self.net_config.get("proxy_url", "")
            if p:
                s.proxies = {"http": p, "https": p}
                self.proxy = p
        if not self.proxy:
            self.proxy = (os.environ.get("https_proxy") or os.environ.get("http_proxy")
                          or os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY"))
        s.headers.update({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.zerochan.net/",
        })
        # every curl request (search, probe, download) joins the global ISP pace
        self.curl_session = pace_session(s)
        return self.curl_session

    async def _create_session(self) -> Any:
        """Override: create curl_cffi session instead of aiohttp."""
        self._ensure_curl_session()
        if not getattr(self, "proxy", None):
            self.proxy = None
        return _DummyAsyncSession()

    def _verify_flag(self):
        return bool(self.net_config.get("verify_tls", True))

    def _blocking_get(self, url, headers=None, timeout=30, allow_redirects=True):
        session = self._ensure_curl_session()
        kwargs = {"headers": headers or {}, "timeout": timeout,
                  "allow_redirects": allow_redirects, "verify": self._verify_flag()}
        return session.get(url, **kwargs)

    async def _curl_get_text(self, url, headers=None, timeout=30):
        resp = await asyncio.to_thread(self._blocking_get, url, headers, timeout, True)
        status = int(getattr(resp, "status_code", 0) or 0)
        try:
            text = resp.text or ""
        except Exception:
            try:
                text = resp.content.decode("utf-8", errors="ignore")
            except Exception:
                text = ""
        return status, text, resp

    async def _try_login(self):
        """Best-effort Zerochan login via curl_cffi. Never raises."""
        if not (self._zerochan_user and self._zerochan_pass):
            return False
        try:
            self.log("Authenticating with Zerochan...")
            _status, text, _resp = await self._curl_get_text(
                "https://www.zerochan.net/login", timeout=30)
            token = None
            try:
                m = re.search(r'name=["\'](?:csrf[_-]?token|token)["\'][^>]*value=["\']([^"\']+)["\']',
                              text or "", re.IGNORECASE)
                if m:
                    token = m.group(1)
            except Exception:
                token = None
            # Field set mirrors the proven gallery-dl login (ref/name/password/login).
            payload = {"ref": "/", "name": self._zerochan_user,
                       "password": self._zerochan_pass, "login": "Login"}
            if token:
                payload["csrf_token"] = token
            session = self._ensure_curl_session()
            r = await asyncio.to_thread(
                session.post, "https://www.zerochan.net/login",
                data=payload, timeout=30, allow_redirects=True,
                headers={"Referer": "https://www.zerochan.net/login",
                         "Origin": "https://www.zerochan.net"},
                verify=self._verify_flag())
            body = ""
            try:
                body = (r.text or "")[:4000].lower()
            except Exception:
                pass
            ok = (int(getattr(r, "status_code", 0) or 0) in (200, 302)
                  and (self._zerochan_user.lower() in body or "logout" in body))
            if ok:
                self.log("Zerochan login looks successful.")
            else:
                self.log("Zerochan login uncertain — continuing (anonymous limits may apply).")
            return bool(ok)
        except Exception as e:
            self.log(f"Zerochan login failed ({e}) — continuing anonymously.")
            return False

    # --- ENGINE 1: gallery-dl with the user's login (more images than anon) ---
    _gallery_dl_cmd = None  # cached launcher: [python, -m, gallery_dl] or [binary]

    def _gallery_dl_launcher(self):
        """Resolve how to invoke gallery-dl, preferring the current interpreter."""
        if ZerochanWorker._gallery_dl_cmd is not None:
            return ZerochanWorker._gallery_dl_cmd
        launcher = None
        # A frozen executable is the application, not a Python interpreter:
        # invoking it with -m would launch another application instance.
        if not getattr(sys, 'frozen', False):
            try:
                probe = subprocess.run(
                    [sys.executable, "-m", "gallery_dl", "--version"],
                    capture_output=True, text=True, timeout=15, check=False)
                if probe.returncode == 0:
                    launcher = [sys.executable, "-m", "gallery_dl"]
            except Exception:
                launcher = None
        if launcher is None and shutil.which("gallery-dl"):
            launcher = ["gallery-dl"]
        ZerochanWorker._gallery_dl_cmd = launcher
        return launcher

    def _gallery_dl_enumerate(self, tag, page=1, page_size=10):
        """One page of posts via gallery-dl using the Settings login.

        gallery-dl lists the whole tag (up to the run's cap) in a single
        process, so the listing is fetched once and later pages are sliced
        from it. Returns (posts, engine_ok); engine_ok False means
        gallery-dl itself failed and the caller must switch engines.
        """
        listing = getattr(self, "_gdl_listing", None)
        if listing is None:
            # always list the full cap: already-downloaded posts are filtered
            # out below, so a smaller listing would starve a small amount
            posts, engine_ok = self._gallery_dl_list(
                tag, getattr(self, "_gdl_limit", 200))
            if not engine_ok:
                return [], False
            listing = posts
            self._gdl_listing = listing
        start = (page - 1) * page_size
        return listing[start:start + page_size], True

    def _gallery_dl_list(self, tag, limit):
        """Run gallery-dl once for `limit` posts of the tag."""
        username = self._zerochan_user
        password = self._zerochan_pass
        if not (username and password):
            return [], False

        launcher = self._gallery_dl_launcher()
        if launcher is None:
            self.log("gallery-dl not found (no python module, no binary on PATH).")
            return [], False

        base_cmd = list(launcher)
        gdl_configs = []
        # Credentials go through a temp gallery-dl config file, never argv —
        # process arguments are readable by any local user via ps.
        cfg = tempfile.NamedTemporaryFile(
            "w", suffix=".json", prefix="rems-gdl-", delete=False)
        try:
            _json.dump({"extractor": {"zerochan": {
                "username": username, "password": password}}}, cfg)
            cfg.close()
            base_cmd.extend(["--config", cfg.name])
            gdl_configs.append(cfg.name)
        except Exception:
            cfg.close()
            try:
                os.unlink(cfg.name)
            except OSError:
                pass
            raise

        if self.net_config.get("use_proxy"):
            base_cmd.extend(["--proxy", self.net_config["proxy_url"]])

        # Listing only: the whole tag in one request. The per-post page fetch
        # done by the caller then supplies the exact URL and categorized tags,
        # which keeps gallery-dl's access while skipping its per-post passes.
        base_cmd.append("-j")

        self.log(f"Listing '{self.original_tag}' with gallery-dl "
                 f"(up to {limit} posts)...")
        cmd = base_cmd + [
            "--range", f"1-{limit}",
            f"https://www.zerochan.net/{tag}",
        ]

        proc = None
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
            watchdog = threading.Timer(600, proc.kill)
            watchdog.start()
            try:
                stdout, stderr = proc.communicate(timeout=580)
            finally:
                watchdog.cancel()
                for cfg_path in gdl_configs:
                    try:
                        os.unlink(cfg_path)
                    except OSError:
                        pass

            if proc.returncode != 0:
                err_tail = (stderr or "").strip().splitlines()[-1:] or [""]
                self.log(f"gallery-dl listing exited ({proc.returncode}): "
                         f"{err_tail[0][:220]}")
                fatal_markers = ("no such option", "not recognized", "no module",
                                 "cookies-from-browser", "authentication", "cloudflare",
                                 "429", "403", "blocked")
                if any(m in (stderr or "").lower() for m in fatal_markers):
                    return [], False
                return [], True

            try:
                data = _json.loads(stdout or "[]")
            except Exception as e:
                self.log(f"gallery-dl listing: bad JSON ({e}).")
                return [], True

            # gallery-dl -j reports failures as [-1, {error...}] and still
            # exits 0 — that is an engine failure, not "no posts found".
            for msg in data:
                if isinstance(msg, list) and msg and msg[0] == -1:
                    info = msg[1] if len(msg) > 1 and isinstance(msg[1], dict) else {}
                    detail = info.get("message") or info.get("error") or "unknown error"
                    self.log(f"gallery-dl listing error: {str(detail)[:220]}")
                    return [], False

            posts = []
            seen = set()
            for msg in data:
                if not isinstance(msg, list) or len(msg) < 2:
                    continue
                msg_type = msg[0]
                if msg_type == 3 and len(msg) >= 3:
                    kwdict = msg[2] if isinstance(msg[2], dict) else {}
                elif msg_type == 2 and isinstance(msg[1], dict):
                    kwdict = msg[1]
                else:
                    continue
                pid = kwdict.get("id")
                if pid is None or pid in seen:
                    continue
                seen.add(pid)
                posts.append(kwdict)

            return posts, True

        except FileNotFoundError:
            self.log("gallery-dl binary not found on PATH.")
            return [], False
        except Exception as e:
            if proc is not None and proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    pass
            self.log(f"gallery-dl listing failed: {e}")
            return [], False

    # --- per-post page: categorized tags + the real full-image URL ---
    @staticmethod
    def _extract_jsonld(html):
        """JSON-LD block carrying ``contentUrl`` (the exact full-image URL)."""
        for block in re.findall(
                r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                html or "", re.S):
            try:
                data = _json.loads(block)
            except Exception:
                continue
            if isinstance(data, dict) and data.get("contentUrl"):
                return data
        return None

    async def _fetch_post_page(self, pid):
        """One GET of the post page: categorized tags + exact full URL.

        Replaces the gallery-dl dependency (which spent two requests per post
        and needed a patched site-packages extractor). Returns ("", "") on
        failure so the caller can fall back to the static-URL probe.
        """
        url = f"https://www.zerochan.net/{pid}"
        try:
            session = self._ensure_curl_session()

            def _do():
                return session.get(url, timeout=30, allow_redirects=True,
                                   headers={"Referer": "https://www.zerochan.net/",
                                            "User-Agent": self._api_user_agent()},
                                   verify=self._verify_flag())

            resp = await asyncio.to_thread(_do)
            status = int(getattr(resp, "status_code", 0) or 0)
            try:
                html = resp.text or ""
            except Exception:
                html = (getattr(resp, "content", b"") or b"").decode("utf-8", "ignore")
            if status != 200 or not html:
                self.log(f"Post {pid}: page fetch failed (HTTP {status}).")
                return "", ""
            jsonld = self._extract_jsonld(html)
            return html, ((jsonld or {}).get("contentUrl") or "")
        except Exception as e:
            self.log(f"Post {pid}: page fetch failed ({e}).")
            return "", ""

    def _api_user_agent(self):
        # Zerochan asks API clients to identify themselves: "app - username"
        return f"Rems-Dl - {self._zerochan_user or 'anonymous'}"

    # --- ENGINE 2: built-in Zerochan ?json API via curl_cffi ---
    async def _curl_json_enumerate(self, page=1, per_page=200):
        """List posts via https://www.zerochan.net/{tag}?json=1&l=N&p=M.

        Zerochan's own listing API (the same one its pages use).
        Returns (posts, engine_ok): engine_ok False means CF/HTTP/parse
        failure — the caller must NOT treat that as end-of-data. The API
        exposes no `next` flag, so an empty list with engine_ok True is
        the real end of the tag (advance p until a page comes back empty).
        """
        params = {"json": "1", "l": per_page, "p": page}
        try:
            session = self._ensure_curl_session()
            qs = urllib.parse.urlencode(params)
            url = f"https://www.zerochan.net/{self.encoded_tag}?{qs}"

            def _do():
                return session.get(url, timeout=30, allow_redirects=True,
                                   headers={"Referer": "https://www.zerochan.net/",
                                            "Accept": "application/json",
                                            # zerochan lets API clients through;
                                            # a browser UA gets the "Checking
                                            # browser..." interstitial instead
                                            "User-Agent": self._api_user_agent()},
                                   verify=self._verify_flag())

            resp = await asyncio.to_thread(_do)
            status = int(getattr(resp, "status_code", 0) or 0)
            raw = getattr(resp, "content", b"") or b""
            if _looks_like_cf_challenge(status, raw.decode("utf-8", "ignore")):
                self.log(f"Cloudflare challenge on JSON page {page} (HTTP {status}).")
                return [], False
            # zerochan's template sometimes breaks: HTTP 500 carrying the
            # real object wrapped in junk ("{\r\n{...items...}\r\n</ul>\r\n}")
            if not raw or (status != 200 and b'"items"' not in raw):
                self.log(f"JSON listing page {page} failed: HTTP {status}.")
                return [], False
            try:
                text = raw.decode("utf-8", "ignore")
                try:
                    data = _json.loads(text)
                except Exception:
                    # re-anchor on the payload and stop at the object's end,
                    # ignoring the ad script / broken-markup wrapper
                    pos = text.find('"items"')
                    if pos < 0:
                        raise
                    data = _json.JSONDecoder().raw_decode("{" + text[pos:])[0]
            except Exception as e:
                self.log(f"JSON listing page {page}: bad JSON ({e}).")
                return [], False
            items = data.get("items", []) if isinstance(data, dict) else []
            return items, True
        except Exception as e:
            self.log(f"JSON listing page {page} failed: {e}")
            return [], False

    # --- ENGINE 3: RSS/XML feed (when the ?json listing is broken) ---
    async def _curl_xml_enumerate(self, page=1):
        """List posts via https://www.zerochan.net/{tag}?xml=1&p=N.

        Fixed at 100 posts per page, no md5, and an empty page is the real
        end of the tag. Returns (posts, engine_ok): engine_ok False means
        CF/HTTP/parse failure, never end-of-data.
        """
        url = f"https://www.zerochan.net/{self.encoded_tag}?xml=1&p={page}"
        media = "{http://search.yahoo.com/mrss/}"
        try:
            session = self._ensure_curl_session()
            ua = self._api_user_agent()

            def _do():
                return session.get(url, timeout=30, allow_redirects=True,
                                   headers={"Referer": "https://www.zerochan.net/",
                                            "Accept": "application/xml",
                                            "User-Agent": ua},
                                   verify=self._verify_flag())

            resp = await asyncio.to_thread(_do)
            status = int(getattr(resp, "status_code", 0) or 0)
            raw = getattr(resp, "content", b"") or b""
            if _looks_like_cf_challenge(status, raw.decode("utf-8", "ignore")):
                self.log(f"Cloudflare challenge on XML page {page} (HTTP {status}).")
                return [], False
            if status != 200 or not raw:
                self.log(f"XML listing page {page} failed: HTTP {status}.")
                return [], False
            try:
                root = ET.fromstring(raw)
            except Exception as e:
                self.log(f"XML listing page {page}: bad XML ({e}).")
                return [], False

            posts = []
            for item in root.iter("item"):
                src = (item.findtext("guid") or item.findtext("link") or "").rstrip("/")
                pid = src.rsplit("/", 1)[-1]
                if not pid.isdigit():
                    continue
                post: dict[str, object] = {"id": int(pid)}
                content = item.find(media + "content")
                if content is not None:
                    for key in ("width", "height"):
                        try:
                            post[key] = int(content.get(key) or 0)
                        except (TypeError, ValueError):
                            pass
                kw = (item.findtext(media + "keywords") or "")
                tags = [" ".join(t.split()) for t in kw.split(",")]
                post["tags"] = [t for t in tags if t][:60]
                posts.append(post)
            return posts, True
        except Exception as e:
            self.log(f"XML listing page {page} failed: {e}")
            return [], False

    async def _probe_static_url(self, pid):
        """Return the first existing static.zerochan.net full URL for a post id."""
        # ponytail: up to 4 sequential 1-byte probes per id-only post —
        # probe a learned ext map first if enumeration throughput ever matters
        session = self._ensure_curl_session()

        def _probe(url):
            try:
                return session.get(
                    url, timeout=15, allow_redirects=True,
                    headers={"Referer": "https://www.zerochan.net/",
                             "Range": "bytes=0-0"},
                    verify=self._verify_flag())
            except Exception:
                return None

        for ext in ("jpg", "png", "webp", "gif"):
            cand = f"https://static.zerochan.net/.full.{pid}.{ext}"
            try:
                resp = await asyncio.to_thread(_probe, cand)
                if resp is None:
                    continue
                status = int(getattr(resp, "status_code", 0) or 0)
                if status in (200, 206):
                    ctype = ""
                    try:
                        ctype = str(resp.headers.get("content-type", "")).lower()
                    except Exception:
                        ctype = ""
                    if "html" not in ctype:
                        return cand
            except Exception:
                continue
            if self.stop_event.is_set():
                break
        return None

    @staticmethod
    def _split_category_tags(raw_tags):
        """Split ['Category:Name', ...] into (general, artists, chars, copyrights, meta, outfits)."""
        general, artists, characters, copyrights, metadata_tags, outfits = [], [], [], [], [], []
        for entry in raw_tags or []:
            try:
                text = str(entry).strip()
                if not text:
                    continue
                if ":" not in text:
                    general.append(text)
                    continue
                cat, _, name = text.partition(":")
                name = name.strip()
                if not name:
                    continue
                c = cat.strip().lower()
                if c in ("mangaka", "artist"):
                    artists.append(name)
                elif c in ("character",):
                    characters.append(name)
                elif c in ("game", "series", "copyright", "studio"):
                    copyrights.append(name)
                elif c in ("meta", "metadata"):
                    metadata_tags.append(name)
                elif c in ("outfit",):
                    outfits.append(name)
                else:
                    general.append(name)
            except Exception:
                continue
        return general, artists, characters, copyrights, metadata_tags, outfits

    def _already_have(self, post):
        """True when the listing alone proves this post is already on disk.

        Checking before the post-page fetch keeps a tag the user already
        downloaded from spending one request per known post.
        """
        url = post.get("file_url") or post.get("full") or ""
        name = urllib.parse.unquote(str(url).split("?")[0].split("/")[-1]).lstrip(".")
        if not name:
            name = str(post.get("filename") or "").strip()
        if not name:
            return False
        try:
            return (name in self.dl_history or name in self.queued_items
                    or os.path.exists(os.path.join(self.tag_dir, name)))
        except Exception:
            return False

    async def _enqueue_post_dict(self, post):
        """Normalize a listing post (JSON or XML) and enqueue. Returns bool."""
        try:
            pid = post.get("id")
            img_url = post.get("file_url") or post.get("full")
            if not img_url:
                img_url = _derive_img_url(post)
            if not img_url and pid is not None:
                # Static fallback chain (same idea as patched extractor _urls):
                # probe which extension actually exists with a 1-byte request.
                img_url = await self._probe_static_url(pid)
            if not img_url:
                self.log(f"No image URL found for post {pid}, skipping.")
                return False

            page_html = post.get("page_html", "") or ""
            if page_html:
                categorized = parse_zerochan_tags(page_html)
                artists = [t["tag"] for t in categorized if t["category"] in ("mangaka",)]
                characters = [t["tag"] for t in categorized if t["category"] in ("character",)]
                copyrights = [t["tag"] for t in categorized if t["category"] in ("game", "series", "studio")]
                metadata_tags = [t["tag"] for t in categorized if t["category"] in ("meta",)]
                outfits = [t["tag"] for t in categorized if t["category"] in ("outfit",)]
                tags_list = [t["tag"] for t in categorized
                             if t["category"] in ("theme", "source", "vtuber",
                                                  "group")]
            else:
                tags_raw = post.get("tags", [])
                if tags_raw and isinstance(tags_raw, list) and any(":" in str(t) for t in tags_raw):
                    tags_list, artists, characters, copyrights, metadata_tags, outfits = \
                        self._split_category_tags(tags_raw)
                elif isinstance(tags_raw, str):
                    tags_list = [t.strip() for t in tags_raw.replace(",", " ").split() if t.strip()]
                    artists, characters, copyrights, metadata_tags, outfits = [], [], [], [], []
                else:
                    tags_list = [str(t).strip() for t in (tags_raw or []) if str(t).strip()]
                    artists, characters, copyrights, metadata_tags, outfits = [], [], [], [], []

            filename = urllib.parse.unquote(img_url.split('?')[0].split('/')[-1])
            # static URLs are ".full.N.ext" — strip the leading dot or every
            # download lands as a hidden file
            filename = filename.lstrip(".")
            if not filename or '.' not in filename:
                filename = f"zerochan_{pid}.jpg"
            # ':' and other reserved chars break Windows renames (WinError 87)
            filename = sanitize_filename(filename, fallback=f"zerochan_{pid}.jpg")
            filepath = os.path.join(self.tag_dir, filename)

            return await self.enqueue_download(
                img_url, filepath, filename, tags_list,
                artists=artists, characters=characters,
                copyrights=copyrights, metadata_tags=metadata_tags,
                outfits=outfits, source_md5=post.get("md5"))
        except Exception as e:
            self.log(f"Error processing post {post.get('id', '?')}: {e}")
            return False

    # --- queue (overrides aiohttp HEAD size lookup) ---
    async def enqueue_download(self, url, filepath, filename, tags_list, artists=None,
                               characters=None, copyrights=None, metadata_tags=None,
                               outfits=None, groups=None, hair=None, eyes=None,
                               rating=None, source_md5=None):
        if artists is None:
            artists = []
        if outfits is None:
            outfits = []
        try:
            if filename in self.dl_history or filename in self.queued_items \
                    or os.path.exists(filepath):
                return False
        except Exception as e:
            self.log(f"Pre-download dedup check failed for {filename}: {e}")
        # Same bytes under a different filename ("Name.full.ID.ext" from the
        # page vs ".full.ID.ext" from the probe) are dropped before transfer.
        if not await asyncio.to_thread(
                self._claim_source_md5, filepath, source_md5, tags_list, artists,
                characters, copyrights, metadata_tags, outfits):
            return False
        # Skip HEAD entirely: Zerochan + Cloudflare often blocks HEAD, and the
        # size is only used for progress stats. Record 0 and continue.
        file_size = 0
        self.queued_items.add(filename)
        # base's backpressure: a full queue must stay responsive to Stop,
        # and a dropped item must not stay in queued_items forever
        item = (url, filepath, filename, tags_list, artists, file_size,
                characters, copyrights, metadata_tags, outfits)
        queue = self.download_queue
        if queue is None:
            self.queued_items.discard(filename)
            return False
        while not self.stop_event.is_set():
            try:
                await asyncio.wait_for(queue.put(item), 0.25)
                self.enqueued_count += 1
                return True
            except asyncio.TimeoutError:
                continue
        self.queued_items.discard(filename)
        return False

    # --- downloader (overrides aiohttp streaming) ---
    async def _async_download_file(self, url, filepath, filename, tags_list, artists,
                                   file_size=0, characters=None, copyrights=None,
                                   metadata_tags=None, outfits=None, groups=None,
                                   hair=None, eyes=None, rating=None):
        if artists is None:
            artists = []
        if outfits is None:
            outfits = []
        if self.stop_event.is_set():
            self.enqueued_count -= 1
            return False

        part_path = filepath + ".part"
        safe_ensure_dir(os.path.dirname(filepath))
        loop = asyncio.get_running_loop()
        for attempt in range(self.dl_retries):
            try:
                if self.stop_event.is_set():
                    break
                # first attempt stays quiet — [SUCCESS]/[FAILED] report the
                # outcome; only retries earn a log line
                if attempt:
                    self.log(f"Downloading {filename} (attempt {attempt + 1}/{self.dl_retries})...")

                def _do_download():
                    session = self._ensure_curl_session()
                    # stream to .part so the bar moves while bytes arrive;
                    # curl_cffi maps the tuple's second value to the LOW_SPEED
                    # stall bound, so a dead transfer can't hang for minutes
                    resp = session.get(
                        url, timeout=(10, 30), allow_redirects=True, stream=True,
                        headers={"Referer": "https://www.zerochan.net/"},
                        verify=self._verify_flag())
                    status = int(getattr(resp, "status_code", 0) or 0)
                    if status != 200:
                        resp.close()
                        raise Exception(f"HTTP {status}")
                    try:
                        total = int(resp.headers.get("Content-Length", 0) or 0)
                    except Exception:
                        total = 0
                    ctype = ""
                    try:
                        ctype = str(resp.headers.get("content-type", "")).lower()
                    except Exception:
                        ctype = ""
                    chunks, got = [], 0
                    try:
                        with open(part_path, "wb") as f:
                            for chunk in resp.iter_content(65536):
                                if not chunk:
                                    continue
                                if self.stop_event.is_set():
                                    break
                                f.write(chunk)
                                chunks.append(chunk)
                                got += len(chunk)
                                now = time.monotonic()
                                if total and now - self._last_progress_emit >= 0.2:
                                    self._last_progress_emit = now
                                    self._inflight[filepath] = got / total
                                    loop.call_soon_threadsafe(self._emit_progress)
                    finally:
                        resp.close()
                    return ctype, b"".join(chunks)

                ctype, content = await asyncio.to_thread(_do_download)
                if len(content) <= 1000:
                    raise Exception(f"file too small ({len(content)} bytes)")
                # Cloudflare sometimes returns an HTML challenge with 200
                if "text/html" in ctype and b"Just a moment" in content[:8192]:
                    raise Exception("Cloudflare challenge (HTTP 200 with JS check) — retrying")

                if self.stop_event.is_set():
                    if os.path.exists(part_path):
                        os.remove(part_path)
                    self.enqueued_count -= 1
                    return False

                os.replace(part_path, filepath)

                # persistent perceptual-hash dedup (see core/shared.py); all
                # formats of the bytes just served are the site's own hashes
                generated = hash_bytes(content)
                dup = await asyncio.to_thread(check_duplicate, filepath, self.name,
                                              source_md5=generated["md5"],
                                              generated_hashes=generated)
                if dup is not None and dup.is_duplicate:
                    try:
                        os.remove(filepath)
                    except OSError:
                        pass
                    self.enqueued_count -= 1
                    self.duplicate_count += 1
                    await asyncio.to_thread(self._remember_filename, filename)
                    await asyncio.to_thread(absorb_duplicate, dup, self.name, tags_list,
                                            artists, characters, copyrights,
                                            metadata_tags, outfits)
                    return False

                self.downloaded_count += 1
                # the post that produced this file is no longer "new" — drop
                # its watcher notification (URL/filename carry the site's
                # hash or the post id, so no post_id plumbing is needed)
                try:
                    from core import notifications as _notif
                    await asyncio.to_thread(_notif.drop_downloaded,
                                            _notif.source_for_worker(self.name),
                                            url, filename, generated["md5"])
                except Exception as e:
                    self.log(f"Notification prune skipped: {e}")
                self.dl_history.add(filename)
                await asyncio.to_thread(save_history, self.site_root, self.dl_history)

                if self.is_scanning and self.amount > 0:
                    target_total = max(self.amount, self.enqueued_count)
                else:
                    target_total = max(self.enqueued_count, self.downloaded_count)

                pct = int((self.downloaded_count / target_total) * 100) if target_total > 0 else 0

                try:
                    rel_path = os.path.relpath(filepath, shared.MASTER_FOLDER)
                except ValueError:
                    rel_path = filepath
                top_tags = ", ".join(tags_list[:5]) if tags_list else "No tags"
                tagd = build_tagd(artists, characters, copyrights, metadata_tags, outfits)

                # send_tags first: history row must exist before the image is
                # clickable in the viewer (its tag box reads imageHistory)
                await asyncio.to_thread(send_tags, self.name, filename, tags_list, artists, rel_path,
                          characters, copyrights, metadata_tags, outfits)
                await asyncio.to_thread(write_image_metadata, filepath, tags_list, artists, self.name,
                                     characters, copyrights, metadata_tags, outfits)
                await asyncio.to_thread(add_to_gallery, self.name, filename, rel_path, tags_list, artists,
                               characters, copyrights, metadata_tags, outfits)
                self.log(f"[SUCCESS] Downloaded {filename} ({self.downloaded_count}/{target_total}) [{pct}%] |PATH| {rel_path} |TAGS| {top_tags} |TAGD| {tagd}{dims_log_token(filepath)}")
                return True

            except Exception as e:
                try:
                    if os.path.exists(part_path):
                        os.remove(part_path)
                except Exception:
                    pass
                if self.stop_event.is_set():
                    self.enqueued_count -= 1
                    break
                if attempt < self.dl_retries - 1:
                    await asyncio.sleep(2)
                else:
                    self.enqueued_count -= 1
                    err_msg = str(e).strip() or "HTTP error / File deleted from server"
                    self.log(f"[FAILED] {filename}: {err_msg}")
                    self.failed_count += 1
                    try:
                        if os.path.exists(filepath):
                            os.remove(filepath)
                    except Exception:
                        pass
        return False

    async def scraper_task(self):
        self.log(f"Initializing worker for tag: '{self.original_tag}'")
        self._ensure_curl_session()
        await self._try_login()

        collected_count = 0
        page = 1
        MAX_PAGES = 50
        PAGE_SIZE = 10
        # One gallery-dl process covers the whole run: its listing is sliced
        # locally, so the range follows the requested amount (200 is the floor
        # — known posts are filtered out after listing and must not starve it).
        # 2000 caps one listing walk (~20 requests) for absurd amounts.
        self._gdl_limit = min(max(200, self.amount or 200), 2000)
        if self.amount <= 0 or self.amount > self._gdl_limit:
            self.log(f"gallery-dl lists at most {self._gdl_limit} posts per run; "
                     f"re-run to continue past that.")
        # gallery-dl first only when it can actually log in: with the user's
        # Zerochan credentials it sees posts the anonymous API hides, and that
        # is the whole reason it stays the preferred engine.
        use_gallery_dl = bool(self._zerochan_user and self._zerochan_pass)
        self._gdl_listing = None
        use_xml = False
        # Fixed JSON page size so ?p= pagination never overlaps/skips.
        json_per_page = max(24, min(200, self.amount if self.amount > 0 else 200))

        async def _with_metadata(post):
            # the listing carries no full URL and only flat tags: one page
            # fetch supplies both (categorized tags + JSON-LD contentUrl)
            html, full_url = await self._fetch_post_page(post.get("id"))
            if full_url:
                post["file_url"] = full_url
            if html:
                post["page_html"] = html
            return post

        # 4 at a time: one request per post, well inside the 60 req/min
        # allowance while keeping a 100-post page ~10s
        sem = asyncio.Semaphore(4)

        async def _guarded(post):
            async with sem:
                return await _with_metadata(post)

        while page <= MAX_PAGES:
            if self.stop_event.is_set():
                break
            if self.amount > 0 and collected_count >= self.amount:
                break

            if use_gallery_dl:
                # the whole tag is listed once; later pages are local slices
                self.log(f"Scanning gallery-dl... (Page {page})")
                posts, engine_ok = await asyncio.to_thread(
                    self._gallery_dl_enumerate, self.encoded_tag, page, PAGE_SIZE)
                if not engine_ok:
                    self.log("gallery-dl engine unavailable — switching "
                             "to the built-in Zerochan API.")
                    use_gallery_dl = False
                    page = (page - 1) * PAGE_SIZE // json_per_page + 1
                    continue
                if not posts:
                    if page == 1:
                        # an empty first page is ambiguous (bad login / silent
                        # extractor failure), not proof the tag is empty
                        self.log("gallery-dl returned nothing — switching "
                                 "to the built-in Zerochan API.")
                        use_gallery_dl = False
                        page = 1
                        continue
                    # before the 200 cap an empty range is the real end of the
                    # tag, not an engine failure
                    self.log("No more posts available.")
                    break
                # a page of already-downloaded posts costs no requests and
                # must not stop the run short of its amount
                posts = [p for p in posts if not self._already_have(p)]
                if not posts:
                    page += 1
                    continue
                if self.amount > 0:
                    posts = posts[:max(1, self.amount - collected_count)]
                posts = await asyncio.gather(*(_guarded(p) for p in posts))
            else:
                if use_xml:
                    self.log(f"Scanning XML feed... (Page {page})")
                    items, engine_ok = await self._curl_xml_enumerate(page=page)
                else:
                    self.log(f"Scanning API... (Page {page})")
                    items, engine_ok = await self._curl_json_enumerate(page=page, per_page=json_per_page)
                    if not engine_ok and page == 1:
                        self.log("JSON API unavailable — falling back to the XML feed.")
                        use_xml = True
                        items, engine_ok = await self._curl_xml_enumerate(page=1)
                if not engine_ok:
                    self.log("Zerochan API failed (Cloudflare/network) — "
                             "stopping enumeration; this is NOT the end of the tag.")
                    break
                if not items:
                    if page == 1:
                        self.log(f"No posts found for '{self.original_tag}'. "
                                 "Check the tag spelling.")
                    else:
                        self.log("No more posts available.")
                    break
                items = [p for p in items if not self._already_have(p)]
                if not items:
                    page += 1
                    continue
                if self.amount > 0:
                    items = items[:max(1, self.amount - collected_count)]
                posts = await asyncio.gather(*(_guarded(p) for p in items))

            for post in posts:
                if self.stop_event.is_set():
                    break
                if self.amount > 0 and collected_count >= self.amount:
                    break
                if await self._enqueue_post_dict(post):
                    collected_count += 1

            page += 1

        # collected_count already counts everything enqueued this run — adding
        # qsize too double-counted every item still sitting in the queue
        actual = collected_count
        if actual == 0:
            self.log("No new images to download.")
        else:
            self.log(f"Finished scanning. Enqueued {actual} item"
                     f"{'s' if actual != 1 else ''}. "
                     "Completing downloads in the background...")



def worker_zerochan(tag, amount, net_config):
    worker = ZerochanWorker(tag, amount, net_config)
    worker.run()
