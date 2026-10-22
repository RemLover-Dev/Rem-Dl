"""Read-only installation checks; does not import/start the app or touch databases."""
import argparse
import importlib.util
import os
import platform
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.runtime_paths import app_base_dir, web_directory, webview_gui


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--headless', action='store_true', help='GUI dependencies are not required')
    args = parser.parse_args()
    failures = 0
    print(f'Python {platform.python_version()} / {platform.system()} {platform.machine()}')
    if sys.version_info < (3, 10):
        print('ERROR: Python 3.10 or newer is required')
        failures += 1
    required = ['flask', 'flask_socketio', 'requests', 'dotenv', 'PIL', 'aiohttp', 'curl_cffi', 'rule34Py']
    if not args.headless:
        required.append('webview')
    for module in required:
        present = importlib.util.find_spec(module) is not None
        print(f'{"OK" if present else "MISSING"}: {module}')
        failures += not present
    try:
        assets = Path(web_directory())
        for name in ('index.html', 'script.js', 'style.css', 'vendor/fonts.css', 'vendor/socket.io.min.js'):
            present = (assets / name).is_file()
            print(f'{"OK" if present else "MISSING"}: web/{name}')
            failures += not present
    except FileNotFoundError as error:
        print(f'ERROR: {error}')
        failures += 1
    data = Path(app_base_dir())
    parent = data
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    if not os.access(parent, os.W_OK):
        print('ERROR: data directory is not writable; set REMS_DATA_DIR to a writable location')
        failures += 1
    print(f'GUI selection: {webview_gui() or "native automatic"}')
    for tool in ('ffmpeg', 'ffprobe'):
        print(f'{"OK" if shutil.which(tool) else "OPTIONAL MISSING"}: {tool}')
    print('GUI bindings/WebView2/WebKit availability still requires a native launch test.')
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
