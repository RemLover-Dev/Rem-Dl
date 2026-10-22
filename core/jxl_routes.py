"""Authenticated JXL tools and browser-compatible comparison/gallery images."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import threading

from flask import jsonify, request, send_file
from core.jxl_service import JxlService

_view_lock = threading.Lock()


def browser_png(path, cache, difference_with=None):
    """One decode at a time, on-demand; persist normalized pixels for old WebViews."""
    source = Path(path)
    identity = []
    for p in (source, Path(difference_with) if difference_with else None):
        if p is not None:
            st = p.stat()
            identity.append(f'{p.resolve()}:{st.st_mtime_ns}:{st.st_size}')
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / (hashlib.sha256('|'.join(identity).encode()).hexdigest() + '.png')
    with _view_lock:
        if target.exists():
            return target
        from core.jxl_resources import estimate_memory, memory_budget
        budget = memory_budget()
        cost = estimate_memory(source, 'display', budget)
        if difference_with:
            cost += estimate_memory(difference_with, 'display', budget)
        if cost > budget:
            raise ValueError('Comparison exceeds the current available-memory budget; converted image is unchanged')
        from core.jxl_codec import normalize
        from PIL import Image, ImageChops
        image = normalize(source)
        if difference_with:
            other = normalize(difference_with)
            if image.size != other.size:
                raise ValueError('Images have different normalized dimensions')
            diff = ImageChops.difference(image.convert('RGBA'), other.convert('RGBA'))
            alpha = diff.getchannel('A')
            image = ImageChops.lighter(diff.convert('RGB'), Image.merge('RGB', (alpha, alpha, alpha)))
        temporary = target.with_suffix('.tmp')
        try:
            image.save(temporary, format='PNG', compress_level=1)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        # Bound optional disk copies. A large original pair is allowed, but old
        # comparisons are discarded. No image is regenerated in an idle loop.
        files = sorted(cache.glob('*.png'), key=lambda p: p.stat().st_mtime, reverse=True)
        total = 0
        for index, p in enumerate(files):
            total += p.stat().st_size
            if p != target and index >= 3 and (index >= 12 or total > 256 * 1024 * 1024):
                p.unlink(missing_ok=True)
    return target


def register(app, master, folder_picker, image_picker, changed, cache, replaced=None):
    service = JxlService(master, changed, replaced)
    app.extensions['rems_jxl'] = service

    @app.route('/api/jxl/status')
    def jxl_status():
        return jsonify(codec_available=service.available(), job=service.snapshot())

    @app.route('/api/jxl/start', methods=['POST'])
    def jxl_start():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(error='Expected conversion settings'), 400
        try:
            return jsonify(job=service.start(data)), 202
        except RuntimeError as exc:
            return jsonify(error=str(exc)), 409
        except (ValueError, OSError) as exc:
            return jsonify(error=str(exc)), 400

    @app.route('/api/jxl/cancel', methods=['POST'])
    def jxl_cancel():
        return jsonify(job=service.cancel())

    @app.route('/api/jxl/browse', methods=['POST'])
    def jxl_browse():
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict) or data.get('kind') not in ('file', 'folder'):
            return jsonify(error='Choose file or folder'), 400
        try:
            path = folder_picker(master()) if data['kind'] == 'folder' else image_picker()
            if path is None:
                return jsonify(cancelled=True)
            if path is False:
                return jsonify(error='No desktop picker is available. Paste the image/folder path instead.'), 501
            service.allow_selection(path)
            return jsonify(path=str(path))
        except Exception:
            return jsonify(error='Unable to open the desktop picker. Paste the image/folder path instead.'), 500

    @app.route('/api/jxl/reveal', methods=['POST'])
    def jxl_reveal():
        path = Path(service.snapshot().get('output', ''))
        if not path.is_dir() or not service.snapshot().get('output'):
            return jsonify(error='No completed replacement yet'), 404
        try:
            if sys.platform == 'win32':
                os.startfile(str(path))
            else:
                subprocess.Popen(['open' if sys.platform == 'darwin' else 'xdg-open', str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return jsonify(success=True)
        except OSError:
            return jsonify(error='Could not open the containing folder. Copy its path from the status below.'), 500

    @app.route('/api/jxl/compare/<token>/<side>')
    def jxl_compare_image(token, side):
        result = service.result(token)
        if result is None or not result.get('comparison_available') or side not in ('original', 'converted', 'difference'):
            return jsonify(error='The temporary original is no longer available. Compare the latest completed replacement.'), 404
        try:
            path = browser_png(result['comparison_source'] if side != 'converted' else result['output'], cache,
                               result['output'] if side == 'difference' else None)
            return send_file(path, mimetype='image/png', conditional=True)
        except Exception:
            return jsonify(error='Unable to decode comparison. Check that both files still exist.'), 400
    return service
