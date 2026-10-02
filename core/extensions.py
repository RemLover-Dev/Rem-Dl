import os
import json
import shutil
import logging
import importlib.util
from typing import Dict, Any, List, Optional
from core.database import _app_base_dir, DATABASE_DIR, DatabaseManager

logger = logging.getLogger("RemsDl.Extensions")

EXTENSIONS_DIR = os.path.join(_app_base_dir(), "extensions")
EXTENSIONS_CONFIG_FILE = os.path.join(DATABASE_DIR, "extensions.json")

# Official catalog of discoverable extensions
OFFICIAL_CATALOG = [
    {
        "id": "rems-watcher",
        "name": "Rems Watcher",
        "author": "RemLover-Dev",
        "repository": "RemLover-Dev/Re-Watch",
        "description": "24/7 background tag monitoring, Pixiv follow tracking, unread counters, and instant notifications.",
        "icon": "bell",
        "homepage": "https://github.com/RemLover-Dev/Re-Watch",
        "recommended": True
    }
]


class ExtensionManager:
    """Manages discovery, lifecycle, loading, and persistence of Rems Dl extensions."""

    def __init__(self, app=None, socketio=None):
        self.app = app
        self.socketio = socketio
        self.active_plugins: Dict[str, Any] = {}
        self.plugin_modules: Dict[str, Any] = {}
        os.makedirs(EXTENSIONS_DIR, exist_ok=True)

    def _load_config(self) -> Dict[str, Any]:
        data = DatabaseManager.load_json(EXTENSIONS_CONFIG_FILE)
        if not isinstance(data, dict):
            data = {}
        data.setdefault("extensions", {})
        return data

    def _save_config(self, data: Dict[str, Any]) -> None:
        DatabaseManager.save_json(EXTENSIONS_CONFIG_FILE, data)

    def is_enabled(self, ext_id: str) -> bool:
        cfg = self._load_config()
        ext_cfg = cfg.get("extensions", {}).get(ext_id, {})
        # default to True if installed and not explicitly disabled
        return ext_cfg.get("enabled", True)

    def set_enabled(self, ext_id: str, enabled: bool) -> None:
        cfg = self._load_config()
        ext_cfg = cfg.setdefault("extensions", {}).setdefault(ext_id, {})
        ext_cfg["enabled"] = bool(enabled)
        self._save_config(cfg)

    def discover_installed(self) -> List[Dict[str, Any]]:
        """Scan extensions directory and load manifest.json from each subfolder."""
        installed = []
        if not os.path.exists(EXTENSIONS_DIR):
            return installed

        for entry in os.listdir(EXTENSIONS_DIR):
            entry_path = os.path.join(EXTENSIONS_DIR, entry)
            if not os.path.isdir(entry_path):
                continue

            manifest_path = os.path.join(entry_path, "manifest.json")
            if not os.path.exists(manifest_path):
                continue

            try:
                with open(manifest_path, "r", encoding="utf-8") as f:
                    manifest = json.load(f)
                if not isinstance(manifest, dict) or "id" not in manifest:
                    continue

                ext_id = manifest["id"]
                manifest["installed"] = True
                manifest["enabled"] = self.is_enabled(ext_id)
                manifest["is_active"] = ext_id in self.active_plugins
                manifest["folder"] = entry_path
                installed.append(manifest)
            except Exception as e:
                logger.error(f"Error reading extension manifest at {manifest_path}: {e}")

        return installed

    def get_catalog(self) -> List[Dict[str, Any]]:
        """Combine official catalog with local installation status."""
        installed_map = {item["id"]: item for item in self.discover_installed()}
        catalog = []

        for item in OFFICIAL_CATALOG:
            cat_item = dict(item)
            ext_id = cat_item["id"]
            if ext_id in installed_map:
                inst = installed_map[ext_id]
                cat_item["installed"] = True
                cat_item["version"] = inst.get("version", "1.0.0")
                cat_item["enabled"] = inst.get("enabled", True)
                cat_item["is_active"] = inst.get("is_active", False)
                cat_item["manifest"] = inst
            else:
                cat_item["installed"] = False
                cat_item["version"] = None
                cat_item["enabled"] = False
                cat_item["is_active"] = False
            catalog.append(cat_item)

        # Also add any installed extension that is not in the official catalog
        for ext_id, inst in installed_map.items():
            if not any(c["id"] == ext_id for c in catalog):
                catalog.append({
                    "id": ext_id,
                    "name": inst.get("name", ext_id),
                    "author": inst.get("author", "Local"),
                    "repository": inst.get("repository", ""),
                    "description": inst.get("description", ""),
                    "icon": inst.get("icon", "puzzle"),
                    "homepage": inst.get("homepage", ""),
                    "installed": True,
                    "version": inst.get("version", "1.0.0"),
                    "enabled": inst.get("enabled", True),
                    "is_active": inst.get("is_active", False),
                    "manifest": inst
                })

        return catalog

    def load_extension(self, ext_id: str) -> bool:
        """Dynamically load and initialize an extension by ID."""
        if ext_id in self.active_plugins:
            return True

        installed = {item["id"]: item for item in self.discover_installed()}
        if ext_id not in installed:
            logger.warning(f"Extension '{ext_id}' not found in installed extensions.")
            return False

        manifest = installed[ext_id]
        entry_point = manifest.get("entry_point", "plugin.py")
        plugin_file = os.path.join(manifest["folder"], entry_point)

        if not os.path.exists(plugin_file):
            logger.error(f"Extension '{ext_id}' entry point missing: {plugin_file}")
            return False

        try:
            module_name = f"rems_ext_{ext_id.replace('-', '_')}"
            spec = importlib.util.spec_from_file_location(module_name, plugin_file)
            if spec is None or spec.loader is None:
                logger.error(f"Cannot create spec for extension '{ext_id}' from {plugin_file}")
                return False

            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            context = {
                "extension_id": ext_id,
                "manifest": manifest,
                "extension_dir": manifest["folder"],
                "database_dir": DATABASE_DIR,
                "logger": logging.getLogger(f"RemsDl.Ext.{ext_id}"),
            }

            plugin_instance = None
            if hasattr(module, "setup"):
                plugin_instance = module.setup(self.app, self.socketio, context)
            elif hasattr(module, "Plugin"):
                plugin_instance = module.Plugin(self.app, self.socketio, context)
                if hasattr(plugin_instance, "setup"):
                    plugin_instance.setup()

            self.plugin_modules[ext_id] = module
            self.active_plugins[ext_id] = plugin_instance or module
            logger.info(f"Extension '{ext_id}' loaded successfully.")
            return True
        except Exception as e:
            logger.exception(f"Failed to load extension '{ext_id}': {e}")
            return False

    def unload_extension(self, ext_id: str) -> bool:
        """Call teardown on plugin instance and remove from active plugins."""
        if ext_id not in self.active_plugins:
            return True

        plugin = self.active_plugins.get(ext_id)
        try:
            if hasattr(plugin, "teardown"):
                plugin.teardown()
            elif hasattr(plugin, "cleanup"):
                plugin.cleanup()
            module = self.plugin_modules.get(ext_id)
            if module and hasattr(module, "teardown"):
                module.teardown()
        except Exception as e:
            logger.error(f"Error during extension '{ext_id}' teardown: {e}")

        self.active_plugins.pop(ext_id, None)
        self.plugin_modules.pop(ext_id, None)
        logger.info(f"Extension '{ext_id}' unloaded.")
        return True

    def load_all_enabled(self) -> Dict[str, bool]:
        """Load all enabled extensions."""
        results = {}
        for ext in self.discover_installed():
            ext_id = ext["id"]
            if ext.get("enabled", True):
                results[ext_id] = self.load_extension(ext_id)
            else:
                results[ext_id] = False
        return results

    def enable_extension(self, ext_id: str) -> bool:
        self.set_enabled(ext_id, True)
        return self.load_extension(ext_id)

    def disable_extension(self, ext_id: str) -> bool:
        self.set_enabled(ext_id, False)
        return self.unload_extension(ext_id)

    def uninstall_extension(self, ext_id: str) -> bool:
        self.unload_extension(ext_id)
        self.set_enabled(ext_id, False)

        installed = {item["id"]: item for item in self.discover_installed()}
        if ext_id in installed:
            folder = installed[ext_id]["folder"]
            try:
                shutil.rmtree(folder)
                logger.info(f"Extension '{ext_id}' folder removed: {folder}")
                return True
            except Exception as e:
                logger.error(f"Failed to remove extension '{ext_id}' folder: {e}")
                return False
        return True

    def get_ui_contributions(self) -> Dict[str, Any]:
        """Collect dynamic navigation tabs, header badges, and UI fragments from active extensions."""
        nav_tabs = []
        panels = []
        for ext_id, manifest in {item["id"]: item for item in self.discover_installed()}.items():
            if ext_id not in self.active_plugins:
                continue

            nav_tab = manifest.get("nav_tab")
            if nav_tab and isinstance(nav_tab, dict):
                nav_tabs.append({
                    "extension_id": ext_id,
                    "id": nav_tab.get("id"),
                    "label": nav_tab.get("label", ext_id),
                    "icon_svg": nav_tab.get("icon_svg", ""),
                    "panel_id": nav_tab.get("panel_id", f"{nav_tab.get('id')}-panel")
                })

            panel_html = manifest.get("panel_html")
            if panel_html:
                panels.append({"extension_id": ext_id, "html": panel_html})

        return {"nav_tabs": nav_tabs, "panels": panels}


# Singleton manager instance
_extension_manager: Optional[ExtensionManager] = None


def get_extension_manager(app=None, socketio=None) -> ExtensionManager:
    global _extension_manager
    if _extension_manager is None:
        _extension_manager = ExtensionManager(app=app, socketio=socketio)
    else:
        if app and not _extension_manager.app:
            _extension_manager.app = app
        if socketio and not _extension_manager.socketio:
            _extension_manager.socketio = socketio
    return _extension_manager
