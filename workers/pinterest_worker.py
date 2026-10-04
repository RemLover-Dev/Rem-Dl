import os, re, json, random
import asyncio
from pathlib import Path
from workers import BaseWorker, sanitize_path_component, safe_ensure_dir
import core.shared as shared


def _disable_brotli():
    """urllib3 advertises 'br' when brotlicffi is installed, but brotlicffi
    crashes decoding Pinterest's chunked brotli responses. Force gzip/deflate.
    ponytail: global patch to requests; harmless for other workers (aiohttp)."""
    import requests.sessions
    _orig = requests.sessions.default_headers
    if getattr(_orig, "_no_br", False):
        return
    def _no_br():
        h = _orig()
        h["Accept-Encoding"] = "gzip, deflate"
        return h
    _no_br._no_br = True
    requests.sessions.default_headers = _no_br


def _launch_with_proxy(orig, net):
    """BrowserType.launch wrapper that injects the *caller's* proxy config.

    An inline closure taking `self` as its first parameter shadowed the
    worker's self and read net_config off BrowserType — every browser login
    died with AttributeError. The wrapper must also be restored by the
    caller, or a later run inherits a stale config."""
    def launch(bt, **kw):
        if net.get("use_proxy") and net.get("proxy_url"):
            kw["proxy"] = {"server": net["proxy_url"]}
        return orig(bt, **kw)
    return launch


def _streamlined_login(driver, email, password, url="https://www.pinterest.com/login/"):
    """Drop-in PlaywrightDriver.login replacement.

    pinterest-dl 1.3.0 waits for #email/#password, but Pinterest's streamlined
    redesign renamed them to #streamlined-login-email/-password — the old
    locator times out after 30s on every login. Same human-like flow, both
    selector sets (one email/password input each on either variant)."""
    import time as _t
    page = driver.page
    print("Navigating to login page...")
    page.goto(url)
    _t.sleep(random.uniform(2, 3))

    print("Filling in email...")
    email_field = page.locator("#email, #streamlined-login-email, input[type='email']").first
    email_field.click()
    _t.sleep(random.uniform(0.1, 0.5))
    email_field.fill(email)

    _t.sleep(random.uniform(0.1, 0.5))
    print("Filling in password...")
    password_field = page.locator("#password, #streamlined-login-password, input[type='password']").first
    password_field.click()
    _t.sleep(random.uniform(0.1, 0.5))
    password_field.fill(password)

    _t.sleep(random.uniform(0.3, 1.0))
    print("Submitting login...")
    try:
        page.locator('button[type="submit"]').first.click()
    except Exception:
        password_field.press("Enter")

    print("Waiting for login to process...")
    page.wait_for_load_state("load")
    _t.sleep(random.uniform(1, 2))
    if "/login" not in page.url:
        print("Login Successful")
    print("(If prompted, please complete any CAPTCHA or 2FA in the browser window)")
    return driver


class PinterestWorker(BaseWorker):
    def __init__(self, url_or_query, amount, is_search, net_config, min_w=0, min_h=0):
        super().__init__("pinterest", "Pinterest", amount, net_config)
        _disable_brotli()
        self.url_or_query = url_or_query
        self.is_search = is_search
        self.min_w = min_w
        self.min_h = min_h

        cleaned = url_or_query.strip().lower().replace("https://", "").replace("http://", "").replace("/", "_")
        safe_name = sanitize_path_component(cleaned[:60], fallback="pinterest_query")
        self.site_root = os.path.join(shared.MASTER_FOLDER, "Pinterest", safe_name)
        safe_ensure_dir(self.site_root)
        self.tag_dir = self.site_root

    async def _create_session(self):
        """Pinterest uses its own library, not aiohttp – return None."""
        self.proxy = None
        if self.net_config.get("use_proxy"):
            self.proxy = self.net_config.get("proxy_url")
        return None

    def _apply_proxy(self, session):
        if self.net_config.get("use_proxy"):
            session.proxies = {"http": self.net_config["proxy_url"], "https": self.net_config["proxy_url"]}
        else:
            session.proxies = {"http": "", "https": "", "no_proxy": "*"}

    def _do_login(self, client, cookies_path, email, password):
        if cookies_path and os.path.exists(cookies_path):
            try:
                client.with_cookies_path(cookies_path)
                self.log("Loaded Pinterest cookies")
                return True
            except Exception as e:
                self.log(f"Cookie load error: {e}")

        if email and password:
            try:
                from pinterest_dl import PinterestDL as PDL
                from playwright.sync_api import BrowserType
                from pinterest_dl.webdriver.playwright_driver import PlaywrightDriver
                orig_launch = BrowserType.launch
                orig_pw_login = PlaywrightDriver.login
                BrowserType.launch = _launch_with_proxy(orig_launch, self.net_config)
                PlaywrightDriver.login = _streamlined_login
                scraper = None
                try:
                    # login() returns PlaywrightDriver (no .close) — keep the
                    # scraper, which owns the browser/playwright processes
                    scraper = PDL.with_browser(browser_type="chromium", headless=True, verbose=False)
                    driver = scraper.login(email, password)
                    cookies = driver.get_cookies(after_sec=7)
                finally:
                    if scraper is not None:
                        scraper.close()
                    BrowserType.launch = orig_launch
                    PlaywrightDriver.login = orig_pw_login
                client.with_cookies(cookies)
                if cookies_path:
                    try:
                        os.makedirs(os.path.dirname(cookies_path) or ".", exist_ok=True)
                        with open(cookies_path, "w") as f:
                            json.dump(cookies, f, indent=2)
                        self.log(f"Saved fresh cookies to {cookies_path}")
                    except Exception as e:
                        self.log(f"Could not save cookies: {e}")
                self.log("Logged in via browser and using fresh cookies")
                return True
            except ImportError:
                self.log("⚠️ Browser auth unavailable (install pinterest-dl[browser] and playwright)")
                self.log("Falling back to public content — downloads may be limited.")
            except Exception as e:
                self.log(f"⚠️ Pinterest login FAILED: {e}")
                self.log("Falling back to public content — downloads may be limited.")
        return False

    def _count_existing(self):
        return len([f for f in os.listdir(self.tag_dir) if re.match(r'^\d+\.[a-z]+$', f)])

    async def scraper_task(self):
        self.log(f"Initializing Pinterest worker for: '{self.url_or_query[:80]}' ({'search' if self.is_search else 'url scrape'})")

        if self.net_config.get("use_proxy"):
            os.environ["HTTP_PROXY"] = self.net_config["proxy_url"]
            os.environ["HTTPS_PROXY"] = self.net_config["proxy_url"]
        else:
            os.environ.pop("HTTP_PROXY", None)
            os.environ.pop("HTTPS_PROXY", None)

        from pinterest_dl import PinterestDL
        from pinterest_dl.download import MediaDownloader

        client = PinterestDL.with_api(timeout=5, verbose=False, ensure_alt=True)

        # every search/scrape API hit builds a fresh Api — pace its session
        _orig_create_api = client._create_api

        def _paced_create_api(url):
            api = _orig_create_api(url)
            shared.pace_session(api._session)
            return api

        client._create_api = _paced_create_api

        cookies_path = self.net_config.get("pinterest_cookies", "")
        email = self.net_config.get("pinterest_email", "")
        password = self.net_config.get("pinterest_password", "")
        # browser launch + cookie-file IO can take minutes — keep it off the loop
        have_auth = await asyncio.to_thread(self._do_login, client, cookies_path, email, password)

        if not have_auth:
            self.log("No auth — public content only")

        existing = await asyncio.to_thread(self._count_existing)
        fetch_num = self.amount + existing

        seen_ids = set()
        collected = []

        def on_progress(media):
            if len(collected) >= self.amount:
                return
            if media.id in seen_ids:
                return
            ext = Path(media.src).suffix.lower() if media.src else ".jpg"
            if os.path.exists(os.path.join(self.tag_dir, f"{media.id}{ext}")):
                return
            seen_ids.add(media.id)
            collected.append(media)
            shared.socketio_emit("pinterest_progress", {
                "index": len(collected),
                "total": self.amount,
                "alt": media.alt or "",
                "id": media.id
            })

        try:
            if self.is_search:
                await asyncio.to_thread(client.search, query=self.url_or_query, num=fetch_num, min_resolution=(self.min_w, self.min_h), on_progress=on_progress)
            else:
                await asyncio.to_thread(client.scrape, url=self.url_or_query, num=fetch_num, min_resolution=(self.min_w, self.min_h), on_progress=on_progress)
        except Exception as e:
            self.log(f"Scrape error: {e}")
            return

        medias = collected[:self.amount]
        if not medias:
            self.log("No media found.")
            return

        self.log(f"Collected {len(medias)} media items. Starting download...")

        dl_retries = int(self.net_config.get("download_retries", 3))
        downloader = MediaDownloader(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            timeout=int(self.net_config.get("api_timeout", 10)),
            max_retries=dl_retries
        )
        self._apply_proxy(downloader.http_client.session)
        # every media request joins the global ISP pace
        shared.pace_session(downloader.http_client.session)

        downloaded = 0
        for i, media in enumerate(medias):
            if self.stop_event.is_set():
                break
            # names are "{id}.{ext}" (see pinterest_dl downloader) — skip pins
            # whose name a previous run downloaded or hash-killed.
            # ponytail: O(history) prefix scan per pin; index by id if histories grow
            if any(n.startswith(f"{media.id}.") for n in self.dl_history):
                continue

            try:
                path = await asyncio.to_thread(downloader.download, media, Path(self.site_root), download_streams=True)
                filename = os.path.basename(path)

                # persistent perceptual-hash dedup (see core/shared.py)
                dup = await asyncio.to_thread(shared.check_duplicate, str(path), self.name, getattr(media, "id", None))
                if dup is not None and dup.is_duplicate:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                    self.duplicate_count += 1
                    self._remember_filename(filename)
                    continue

                rel = os.path.relpath(str(path), shared.MASTER_FOLDER)
                # media.alt is the pin's written description — an explanation,
                # not tags. Record what was searched for instead.
                tags = [self.url_or_query] if self.is_search else []
                artists = []
                await asyncio.to_thread(shared.add_to_gallery, self.name, filename, rel, tags, artists)
                await asyncio.to_thread(shared.send_tags, self.name, filename, tags, artists, rel)
                self._remember_filename(filename)

                downloaded += 1
                self.log(f"[SUCCESS] Downloaded {filename} ({downloaded}/{self.amount}) |PATH| {rel} |TAGS| {', '.join(tags[:5])}")
            except Exception as e:
                self.log(f"[FAILED] {media.id}: {e}")

            await asyncio.sleep(random.uniform(0.5, 1.5))

        # ponytail: stopped runs wind down late — never paint summaries over the next run
        if self.stop_event.is_set():
            return
        self.log(f"Downloaded {downloaded} items.")
        self.downloaded_count = downloaded
        self.failed_count = len(medias) - downloaded

    def run(self):
        # pinterest_dl reads proxy config only from the process env — save
        # and restore it so a pinterest run can't leave its proxy settings
        # glued to every other worker after it finishes
        proxy_keys = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
        saved = {k: os.environ.get(k) for k in proxy_keys}
        try:
            asyncio.run(self.run_async_loop(self.scraper_task))
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

def worker_pinterest(url_or_query, amount, is_search, net_config, min_w=0, min_h=0):
    worker = PinterestWorker(url_or_query, amount, is_search, net_config, min_w, min_h)
    worker.run()
