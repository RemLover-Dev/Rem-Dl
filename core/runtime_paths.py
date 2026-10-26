"""Shared paths for source, frozen and explicitly relocated installations.

Keep existing portable data in place. REMS_DATA_DIR permits a writable data
location for protected installations without changing packaged resource paths.
"""
import os
import sys
import math
from functools import lru_cache


def source_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@lru_cache(maxsize=1)
def app_base_dir():
    override = os.environ.get('REMS_DATA_DIR', '').strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    if getattr(sys, 'frozen', False):
        portable = os.path.dirname(sys.executable)
        # A signed macOS application bundle must not be modified to store data.
        # Preserve an existing portable database if the user already has one.
        if sys.platform == 'darwin' and '.app' + os.sep + 'Contents' + os.sep in portable + os.sep and not os.path.isdir(os.path.join(portable, 'database')):
            return os.path.expanduser('~/Library/Application Support/Rems-Dl')
        return portable
    return source_root()


def resource_root():
    return getattr(sys, '_MEIPASS', source_root())


def web_directory():
    root = resource_root()
    for name in ('web', 'Web'):
        candidate = os.path.join(root, name)
        if os.path.isdir(candidate):
            return candidate
    raise FileNotFoundError('Missing web assets: expected web/ or Web/ beside the application resources')


def webview_gui():
    # Let pywebview choose the native backend (Cocoa on macOS, WebView2 on
    # Windows, an available GTK/Qt backend on Linux). Keep explicit overrides.
    return os.environ.get('REMS_WEBVIEW_GUI') or os.environ.get('PYWEBVIEW_GUI') or None


def initial_window_size(screen_width=None, screen_height=None):
    """Fit a window in logical screen pixels, leaving space for desktop chrome."""
    try:
        width, height = float(screen_width), float(screen_height)
        if not math.isfinite(width) or not math.isfinite(height) or width <= 0 or height <= 0:
            raise ValueError('Invalid screen dimensions')
    except (TypeError, ValueError, OverflowError):
        return 1024, 700
    return max(200, min(1400, int(width * .9))), max(100, min(900, int(height * .85)))
