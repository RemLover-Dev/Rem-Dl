import os
import re
import shutil
import zipfile
import tempfile
import logging
from typing import Dict, Any, Tuple, Optional
import requests

from core.extensions import EXTENSIONS_DIR, get_extension_manager

logger = logging.getLogger("RemsDl.ExtensionDownloader")

GITHUB_API_URL = "https://api.github.com/repos/{repo}/releases/latest"
GITHUB_ARCHIVE_URL = "https://github.com/{repo}/archive/refs/heads/main.zip"


def _get_request_proxies():
    """Obtain proxy config from environment if present."""
    use_proxy = os.getenv("USE_PROXY", "false").lower() == "true"
    proxy_url = os.getenv("PROXY_URL", "").strip()
    if use_proxy and proxy_url:
        return {"http": proxy_url, "https": proxy_url}
    return None


def fetch_release_info(repo: str) -> Dict[str, Any]:
    """Query GitHub API for latest release info or fall back to main branch archive."""
    proxies = _get_request_proxies()
    headers = {"User-Agent": "RemsDl-ExtensionDownloader/1.0"}

    # Attempt 1: GitHub Releases API
    try:
        url = GITHUB_API_URL.format(repo=repo)
        resp = requests.get(url, headers=headers, proxies=proxies, timeout=10)
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
            # 1. Zip-Slip security check
            for member in zip_ref.namelist():
                member_path = os.path.abspath(os.path.join(temp_dir_abs, member))
                if os.path.commonpath([temp_dir_abs, member_path]) != temp_dir_abs:
                    raise SecurityError(f"Malicious archive member detected (Zip-Slip): {member}")

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


def install_from_local_path(local_path: str, ext_id: str) -> Tuple[bool, str]:
    """Install or link an extension from a local directory or zip file."""
    manager = get_extension_manager()
    target_dir = os.path.join(EXTENSIONS_DIR, ext_id)

    try:
        local_path = os.path.abspath(local_path)
        if not os.path.exists(local_path):
            return False, f"Source path does not exist: {local_path}"

        if os.path.isdir(local_path):
            manifest_file = os.path.join(local_path, "manifest.json")
            if not os.path.exists(manifest_file):
                return False, f"manifest.json missing in source directory: {local_path}"

            # Copy directory to target_dir
            if os.path.exists(target_dir):
                shutil.rmtree(target_dir)
            shutil.copytree(local_path, target_dir)
        elif zipfile.is_zipfile(local_path):
            if os.path.exists(target_dir):
                shutil.rmtree(target_dir)
            _safe_extract_zip(local_path, target_dir)
        else:
            return False, f"Unsupported file type: {local_path}"

        # Validate extracted manifest
        installed_manifest = os.path.join(target_dir, "manifest.json")
        if not os.path.exists(installed_manifest):
            return False, "Installed extension is missing manifest.json"

        # Enable and load
        manager.enable_extension(ext_id)
        return True, f"Extension '{ext_id}' installed successfully."
    except Exception as e:
        logger.exception(f"Error during local installation of '{ext_id}': {e}")
        return False, str(e)


def install_from_github(repo: str, ext_id: Optional[str] = None) -> Tuple[bool, str]:
    """Download extension zip from GitHub, verify, extract, and load."""
    manager = get_extension_manager()
    if not ext_id:
        ext_id = repo.split("/")[-1].lower()

    target_dir = os.path.join(EXTENSIONS_DIR, ext_id)
    info = fetch_release_info(repo)
    download_url = info.get("download_url")

    if not download_url:
        return False, f"Could not determine download URL for {repo}"

    proxies = _get_request_proxies()
    headers = {"User-Agent": "RemsDl-ExtensionDownloader/1.0"}

    temp_zip = None
    try:
        logger.info(f"Downloading extension '{ext_id}' from {download_url}...")
        resp = requests.get(download_url, headers=headers, proxies=proxies, stream=True, timeout=30)
        resp.raise_for_status()

        with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tf:
            temp_zip = tf.name
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    tf.write(chunk)

        # Extract to target folder
        if os.path.exists(target_dir):
            manager.unload_extension(ext_id)
            shutil.rmtree(target_dir)

        _safe_extract_zip(temp_zip, target_dir)

        # Validate that manifest.json exists
        manifest_file = os.path.join(target_dir, "manifest.json")
        if not os.path.exists(manifest_file):
            return False, f"Downloaded archive does not contain a valid manifest.json."

        # Enable and load dynamically
        manager.enable_extension(ext_id)
        logger.info(f"Extension '{ext_id}' successfully installed from GitHub.")
        return True, f"Extension '{ext_id}' (version {info.get('version')}) installed successfully."

    except Exception as e:
        logger.exception(f"Failed to install extension '{ext_id}' from {repo}: {e}")
        return False, f"Installation failed: {e}"
    finally:
        if temp_zip and os.path.exists(temp_zip):
            try:
                os.remove(temp_zip)
            except Exception:
                pass


def check_for_updates(ext_id: str, repo: str) -> Dict[str, Any]:
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
    pass
