import os
import re

from core.database import DATABASE_DIR
from core.shared import BaseDownloader, folder_component, media_subdir

# session store lives with the other per-install runtime state, not the cwd
TWSCRAPE_DB = os.path.join(DATABASE_DIR, "x_accounts.db")

_HANDLE_RE = re.compile(r"(?:twitter\.com|x\.com)/@?([A-Za-z0-9_]+)", re.IGNORECASE)


class XWorker(BaseDownloader):
    """Downloads original-size photos from an X (Twitter) profile via twscrape.

    Login uses the user's own account credentials, stored once in
    x_accounts.db (twscrape's session store) — never pasted cookies, never
    the official API. Later runs reuse the stored session.
    """

    def __init__(self, tag, amount, net_config):
        super().__init__("x", "X", amount, net_config)
        self.handle = self.normalize_handle(tag)
        self.profile_dir = os.path.join(
            self.site_root, folder_component(self.handle, fallback=self.handle))

    @staticmethod
    def normalize_handle(raw):
        """Accept profile URLs (twitter.com/x.com) or bare handles."""
        raw = (raw or "").strip()
        if not raw:
            return ""
        m = _HANDLE_RE.search(raw)
        if m:
            return m.group(1)
        return raw.lstrip("@").rstrip("/")

    @staticmethod
    def orig_photo_url(url):
        """Force ?name=orig (original size). Returns (extension, url)."""
        base, _, query = (url or "").partition("?")
        ext = None
        _, _, suffix = base.rpartition(".")
        if suffix and "/" not in suffix and 1 <= len(suffix) <= 5:
            ext = suffix.lower()
        if ext is None:
            m = re.search(r"format=([a-z0-9]+)", query, re.IGNORECASE)
            if m:
                ext = m.group(1).lower()
        ext = ext or "jpg"
        path = base if base.rpartition(".")[2].lower() == ext else f"{base}.{ext}"
        return ext, f"{path}?name=orig"

    @staticmethod
    def build_filename(tweet_id, index, ext):
        return f"{tweet_id}_{index}.{(ext or 'jpg').lower().lstrip('.')}"

    @staticmethod
    def build_tags(tweet):
        """Hashtags (lowercase, no #, deduped). The artist pill already
        carries the creator, so no artist tag here."""
        tags, seen = [], set()
        for t in (tweet.hashtags or []):
            t = (t or "").strip().lstrip("#").lower()
            if t and t not in seen:
                seen.add(t)
                tags.append(t)
        return tags

    def _loguru_sink(self, message):
        # forward twscrape's own logger (rate-limit waits, login failures,
        # blocks) into the worker console so waiting is visible
        self.log(message.record["message"])

    @staticmethod
    def auth_mode(net_config):
        """cookies beats password when both are configured."""
        if (net_config.get("x_cookies") or "").strip():
            return "cookies"
        if (net_config.get("x_username") or "").strip():
            return "password"
        return "none"

    @staticmethod
    def load_cookie_string(val):
        """Accept a cookies file path, a Netscape cookies.txt export, or a
        raw `auth_token=...; ct0=...` / JSON cookie string. Returns the
        string twscrape's parse_cookies understands."""
        val = (val or "").strip()
        if os.path.isfile(val):
            try:
                with open(val, "r", encoding="utf-8", errors="replace") as f:
                    val = f.read().strip()
            except OSError:
                return val
        # Netscape format: domain<TAB>flag<TAB>path<TAB>secure<TAB>expiry<TAB>name<TAB>value
        if "\t" in val and "auth_token=" not in val.split("\n", 1)[0]:
            pairs = []
            for line in val.splitlines():
                line = line.removeprefix("#HttpOnly_")
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) >= 7:
                    pairs.append(f"{parts[5]}={parts[6]}")
            if pairs:
                return "; ".join(pairs)
        return val

    async def _ensure_account(self, pool, mode):
        """Add the .env account to the pool store (idempotent). twscrape
        never updates an existing row, so credentials or proxy changes
        replace the stored copy — that forces one fresh login."""
        username = (self.net_config.get("x_username") or "").strip()
        if mode == "cookies":
            if not username:
                raise ValueError("cookies also need the X Username field "
                                 "(the account the cookies belong to)")
            # upsert: refreshes cookies each run, invalidates the stored
            # session when the user rotates them
            await pool.add_account_cookies(
                username, self.load_cookie_string(self.net_config["x_cookies"]))
            return
        if not username:
            return
        password = self.net_config.get("x_password") or ""
        email = (self.net_config.get("x_email") or "").strip()
        email_password = self.net_config.get("x_email_password") or ""
        stored = await pool.get_account(username)
        if stored is not None and (stored.password, stored.email,
                                   stored.email_password, stored.proxy) != \
                (password, email, email_password, self.proxy):
            await pool.delete_accounts(username)
            stored = None
        if stored is None:
            await pool.add_account(username, password, email, email_password,
                                   proxy=self.proxy)

    async def scraper_task(self):
        if not self.handle:
            self.log("Enter a username first (e.g. @rem or x.com/rem).")
            return
        try:
            from twscrape import GqlFeaturesOutdatedError
        except ImportError:
            self.log("twscrape is not installed — run: pip install twscrape")
            return

        from loguru import logger as tw_logger
        sink_id = tw_logger.add(self._loguru_sink, level="INFO")
        try:
            from twscrape import API, AccountsPool

            mode = self.auth_mode(self.net_config)
            if mode == "none":
                self.log("No X account configured — set X_COOKIES or X_USERNAME, "
                         "X_PASSWORD, X_EMAIL, X_EMAIL_PASSWORD in Settings or .env, "
                         "then run again.")
                return

            pool = AccountsPool(db_file=TWSCRAPE_DB)
            try:
                await self._ensure_account(pool, mode)
            except ValueError as e:
                self.log(f"X cookies not set up: {e}")
                return
            api = API(pool=pool, proxy=self.proxy or None)

            if mode == "cookies":
                self.log(f"Using cookies for @{self.handle} session.")
            else:
                self.log(f"Using saved credentials for @{self.handle}.")

            # login_all skips cookie accounts and accounts that already have
            # a session, so this is a one-time step per password account
            counter = await pool.login_all()
            if counter.get("failed"):
                self.log(f"X login failed for {counter['failed']} account(s) — check credentials. "
                         f"If email verification is needed, run in a terminal: "
                         f"twscrape --db {TWSCRAPE_DB} login_accounts --manual")
            info = await pool.accounts_info()
            if not any(x["logged_in"] for x in info):
                self.log("No X account could log in — fix credentials and run again.")
                return

            self.log(f"Resolving @{self.handle} ...")
            user = await api.user_by_login(self.handle)
            if user is None:
                self.log(f"Profile @{self.handle} not found, private, or suspended.")
                return
            if getattr(user, "protected", False):
                self.log(f"Profile @{self.handle} is private — media is not accessible.")
                return
            self.log(f"Found @{user.username} (id {user.id}) — scanning media tweets...")

            # limit=-1 streams the whole profile; enqueue as tweets arrive
            need = self.amount or 10 ** 9
            collected = 0
            scanned = 0
            async for tweet in api.user_media(user.id, limit=-1):
                if self.stop_event.is_set():
                    break
                scanned += 1
                author = (tweet.user.username or "").lower()
                if author != self.handle.lower() or tweet.retweetedTweet is not None:
                    continue  # retweets / quotes of other people
                photos = tweet.media.photos if tweet.media else []
                tags = self.build_tags(tweet)
                for i, photo in enumerate(photos, 1):
                    if self.stop_event.is_set() or collected >= need:
                        break
                    ext, url = self.orig_photo_url(photo.url)
                    filename = self.build_filename(tweet.id, i, ext)
                    filepath = os.path.join(self.profile_dir, media_subdir(ext), filename)
                    if await self.enqueue_download(url, filepath, filename, tags,
                                                   artists=[user.username]):
                        collected += 1
                if scanned % 25 == 0:
                    self.log(f"Scanned {scanned} media tweets ({collected} images enqueued).")

            if self.stop_event.is_set():
                return
            self.check_amount_warning(collected)
            self.log(f"Scanned {scanned} media tweets; enqueued {collected} "
                     f"photo{'s' if collected != 1 else ''} from @{self.handle}.")
        except GqlFeaturesOutdatedError:
            self.log("twscrape's internal API definitions are outdated — run: pip install -U twscrape")
        finally:
            tw_logger.remove(sink_id)


def worker_x(tag, amount, net_config):
    XWorker(tag, amount, net_config).run()
