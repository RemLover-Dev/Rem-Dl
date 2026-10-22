import json
import stat
import threading
from urllib.parse import urlsplit, urljoin
import logging
import os
import re
import shutil
import tempfile
import zipfile
from typing import Any

import requests

from core.extensions import EXTENSIONS_DIR, get_extension_manager

logger = logging.getLogger("RemsDl.ExtensionDownloader")

GITHUB_API_URL = "https://api.github.com/repos/{repo}/releases/latest"
GITHUB_ARCHIVE_URL = "https://github.com/{repo}/archive/refs/heads/main.zip"


def _get_request_proxies():
    """Obtain proxy config from environment if present."""
    # same accepted spellings as core/md5_refetch._get — 'USE_PROXY=1' must
    # not silently bypass the proxy here
    use_proxy = str(os.getenv("USE_PROXY", "")).lower() in ("1", "true", "yes")
    proxy_url = os.getenv("PROXY_URL", "").strip()
    if use_proxy and proxy_url:
        return {"http": proxy_url, "https": proxy_url}
    return None


def _clean_ext_id(ext_id):
    """Sanitize an extension id into a safe single path component.

    ext_id lands straight in os.path.join(EXTENSIONS_DIR, ext_id) and both
    install paths rmtree(target_dir) first — '../../downloads' or '..' would
    delete directories far outside the extension root.
    """
    ext_id = str(ext_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", ext_id):
        return None
    if ext_id.upper() in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(10)], *[f"LPT{i}" for i in range(10)]}:
        return None
    return ext_id


_INSTALL_LOCK = threading.RLock()
MAX_DOWNLOAD = 64 * 1024 * 1024
MAX_EXTRACTED = 256 * 1024 * 1024


def _github_get(url, **kwargs):
    # Validate every redirect before sending another request.
    hosts = {"github.com", "api.github.com", "codeload.github.com",
             "objects.githubusercontent.com", "release-assets.githubusercontent.com",
             "github-releases.githubusercontent.com"}
    from core.database import get_settings
    kwargs.setdefault("verify", get_settings().get("verify_tls", True))
    for _ in range(6):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in hosts or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise SecurityError("Invalid GitHub download URL")
        response = requests.get(url, allow_redirects=False, **kwargs)
        if response.status_code not in (301, 302, 303, 307, 308):
            return response
        location = response.headers.get("Location", "")
        response.close()
        url = urljoin(url, location)
    raise SecurityError("Too many GitHub redirects")


def fetch_release_info(repo: str) -> dict[str, Any]:
    """Query GitHub API for latest release info or fall back to main branch archive."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or any(part in (".", "..") for part in repo.split("/")):
        raise SecurityError("Use a GitHub owner/repository name")
    proxies = _get_request_proxies()
    headers = {"User-Agent": "RemsDl-ExtensionDownloader/1.0"}

    # Attempt 1: GitHub Releases API
    try:
        url = GITHUB_API_URL.format(repo=repo)
        resp = _github_get(url, headers=headers, proxies=proxies, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            tag = data.get("tag_name", "1.0.0")
            version = tag.lstrip("v")
            zip_url = data.get("zipball_url")
            # Look for an explicit .zip asset
            for asset in data.get("assets", []):
                if asset.get("name", "").endswith(".zip"):
                    zip_url = asset.get("browser_download_url")
                    break

            if zip_url:
                return {
                    "version": version,
                    "download_url": zip_url,
                    "name": data.get("name", tag),
                    "body": data.get("body", ""),
                    "published_at": data.get("published_at", "")
                }
    except Exception as e:
        logger.warning(f"Failed to fetch GitHub release for {repo}: {e}")

    # Fallback: Main branch zipball
    fallback_url = GITHUB_ARCHIVE_URL.format(repo=repo)
    return {
        "version": "latest",
        "download_url": fallback_url,
        "name": f"{repo} (main)",
        "body": "Latest development archive from main branch.",
        "published_at": ""
    }


def _safe_extract_zip(zip_path: str, extract_to: str) -> bool:
    """Extract zip with Zip-Slip path traversal protection and directory un-nesting."""
    extract_to = os.path.abspath(extract_to)
    os.makedirs(extract_to, exist_ok=True)

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_dir_abs = os.path.abspath(temp_dir)
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            members = zip_ref.infolist()
            if len(members) > 10000 or sum(m.file_size for m in members) > MAX_EXTRACTED:
                raise SecurityError("Extension archive exceeds extraction limits")
            seen = set()
            for member in members:
                name = member.filename
                parts = name.rstrip("/").split("/")
                if (not name or "\\" in name or ":" in name or name.startswith("/")
                        or any(p in ("", ".", "..") or p.endswith((" ", ".")) for p in parts)
                        or stat.S_ISLNK(member.external_attr >> 16)):
                    raise SecurityError("Unsafe archive path")
                key = name.rstrip("/").casefold()
                if key in seen:
                    raise SecurityError("Duplicate archive path")
                seen.add(key)
            zip_ref.extractall(temp_dir_abs)

        # 2. Check if archive is wrapped in a single root folder (GitHub standard)
        extracted_entries = [os.path.join(temp_dir_abs, e) for e in os.listdir(temp_dir_abs)]
        source_dir = temp_dir_abs
        if len(extracted_entries) == 1 and os.path.isdir(extracted_entries[0]):
            # Has single root folder, e.g. Rems-Watcher-main
            source_dir = extracted_entries[0]

        # 3. Move contents to target directory
        for item in os.listdir(source_dir):
            src_item = os.path.join(source_dir, item)
            dst_item = os.path.join(extract_to, item)
            if os.path.exists(dst_item):
                if os.path.isdir(dst_item):
                    shutil.rmtree(dst_item)
                else:
                    os.remove(dst_item)
            shutil.move(src_item, dst_item)

    return True


def _install_source(local_path, ext_id):
    manager = get_extension_manager()
    ext_id = _clean_ext_id(ext_id)
    if not ext_id:
        raise SecurityError("Invalid extension ID")
    root = os.path.realpath(EXTENSIONS_DIR)
    os.makedirs(root, exist_ok=True)
    target = os.path.join(root, ext_id)
    if os.path.islink(target):
        raise SecurityError("Extension target cannot be a symbolic link")
    with _INSTALL_LOCK, tempfile.TemporaryDirectory(prefix=".install-", dir=root) as work:
        stage = os.path.join(work, "stage")
        if os.path.isdir(local_path):
            source = os.path.realpath(local_path)
            if os.path.commonpath([root, source]) == source:
                raise SecurityError("Cannot install the extension root or its parent")
            for parent, dirs, files in os.walk(source):
                if any(os.path.islink(os.path.join(parent, n)) for n in dirs + files):
                    raise SecurityError("Local extension cannot contain symbolic links")
            shutil.copytree(source, stage)
        elif zipfile.is_zipfile(local_path):
            if os.path.getsize(local_path) > MAX_DOWNLOAD:
                raise SecurityError("Extension archive exceeds download limit")
            _safe_extract_zip(local_path, stage)
        else:
            raise SecurityError("Source must be an extension directory or ZIP")
        with open(os.path.join(stage, "manifest.json"), encoding="utf-8") as stream:
            manifest = json.load(stream)
        if not isinstance(manifest, dict) or manifest.get("id") != ext_id:
            raise SecurityError("Manifest ID must match the extension ID")
        entry = manifest.get("entry_point", "plugin.py")
        if not isinstance(entry, str):
            raise SecurityError("Invalid extension entry point")
        plugin = os.path.realpath(os.path.join(stage, entry))
        if not plugin.startswith(os.path.realpath(stage) + os.sep) or not os.path.isfile(plugin):
            raise SecurityError("Entry point must be a file inside the extension")
        # Validate first. A malformed archive must never remove a working install.
        previous_enabled = manager.is_enabled(ext_id)
        previous_active = ext_id in manager.active_plugins
        manager.unload_extension(ext_id)
        backup = os.path.join(work, "previous")
        if os.path.exists(target):
            os.replace(target, backup)
        try:
            os.replace(stage, target)
            if not manager.enable_extension(ext_id):
                raise RuntimeError("Extension setup failed")
        except Exception:
            manager.unload_extension(ext_id)
            if os.path.exists(target):
                shutil.rmtree(target)
            if os.path.exists(backup):
                os.replace(backup, target)
                manager.set_enabled(ext_id, previous_enabled)
                if previous_active:
                    manager.load_extension(ext_id)
            else:
                manager.set_enabled(ext_id, False)
            raise
    return True, f"Extension '{ext_id}' installed successfully."


def install_from_local_path(local_path: str, ext_id: str) -> tuple[bool, str]:
    try:
        return _install_source(os.path.abspath(local_path), ext_id)
    except Exception as exc:
        logger.warning("Local extension installation failed: %s", exc)
        return False, str(exc)


def install_from_github(repo: str, ext_id: str | None = None) -> tuple[bool, str]:
    try:
        info = fetch_release_info(repo)
        ext_id = _clean_ext_id(ext_id or repo.split("/")[-1].lower())
        if not ext_id:
            raise SecurityError("Invalid extension ID")
        with tempfile.TemporaryDirectory() as work:
            archive = os.path.join(work, "extension.zip")
            with _github_get(info["download_url"], headers={"User-Agent": "RemsDl-ExtensionDownloader/1.0"},
                             proxies=_get_request_proxies(), stream=True, timeout=30) as response:
                response.raise_for_status()
                total = 0
                with open(archive, "wb") as stream:
                    for chunk in response.iter_content(chunk_size=65536):
                        total += len(chunk)
                        if total > MAX_DOWNLOAD:
                            raise SecurityError("Extension archive exceeds download limit")
                        stream.write(chunk)
            return _install_source(archive, ext_id)
    except Exception as exc:
        logger.warning("GitHub extension installation failed: %s", exc)
        return False, str(exc)


def check_for_updates(ext_id: str, repo: str) -> dict[str, Any]:
    """Compare local extension version against latest GitHub release."""
    manager = get_extension_manager()
    installed = {item["id"]: item for item in manager.discover_installed()}

    if ext_id not in installed:
        return {"has_update": False, "installed_version": None, "latest_version": None}

    current_ver = installed[ext_id].get("version", "0.0.0")
    release_info = fetch_release_info(repo)
    latest_ver = release_info.get("version", current_ver)

    # Basic semantic comparison
    has_update = False
    try:
        def parse_v(v_str):
            nums = re.findall(r'\d+', str(v_str))
            return [int(n) for n in nums] if nums else [0]
        has_update = parse_v(latest_ver) > parse_v(current_ver)
    except Exception:
        has_update = (latest_ver != current_ver and latest_ver != "latest")

    return {
        "has_update": has_update,
        "installed_version": current_ver,
        "latest_version": latest_ver,
        "release_notes": release_info.get("body", "")
    }


class SecurityError(Exception):
    """Raised when an archive operation violates filesystem security bounds."""
