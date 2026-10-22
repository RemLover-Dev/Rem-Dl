"""Bounded parallel conversion queue; cancellation owns staging and native process."""
import atexit
import copy
import importlib.util
import json
import hashlib
import shutil
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time

from core.runtime_paths import resource_root

ENCODE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp'}
MAX_FILES = 100000


def install_file(staged, directory, name):
    # Match codec no-clobber publishing without loading the optional native codec
    # in the app server (a native codec failure stays inside the worker).
    import shutil
    directory.mkdir(parents=True, exist_ok=True)
    stem, extension = os.path.splitext(name)
    for index in range(10000):
        target = directory / (name if index == 0 else f'{stem} ({index}){extension}')
        try:
            os.link(staged, target)
            return target
        except FileExistsError:
            continue
        except OSError:
            try:
                stream = target.open('xb')
            except FileExistsError:
                continue
            try:
                with stream, open(staged, 'rb') as source:
                    shutil.copyfileobj(source, stream, 1024 * 1024)
                    stream.flush()
                    os.fsync(stream.fileno())
                return target
            except BaseException:
                target.unlink(missing_ok=True)
                raise
    raise ValueError('Too many output name collisions')


def file_hash(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def file_version(path):
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _copy_windows_creation_time(source, target):
    """Copy the native creation timestamp without treating Unix ctime as birth."""
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    pointer = ctypes.POINTER(wintypes.FILETIME)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetFileTime.argtypes = [wintypes.HANDLE, pointer, pointer, pointer]
    kernel.GetFileTime.restype = wintypes.BOOL
    kernel.SetFileTime.argtypes = [wintypes.HANDLE, pointer, pointer, pointer]
    kernel.SetFileTime.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    invalid = ctypes.c_void_p(-1).value
    handles = []
    try:
        for path, access in ((source, 0x80), (target, 0x100)):
            handle = kernel.CreateFileW(str(path), access, 7, None, 3, 0x80, None)
            if handle == invalid:
                raise ctypes.WinError(ctypes.get_last_error())
            handles.append(handle)
        created = wintypes.FILETIME()
        if not kernel.GetFileTime(handles[0], ctypes.byref(created), None, None):
            raise ctypes.WinError(ctypes.get_last_error())
        if not kernel.SetFileTime(handles[1], ctypes.byref(created), None, None):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        for handle in reversed(handles):
            kernel.CloseHandle(handle)


def preserve_file_dates(source, target, original_stat):
    # Gallery rescans and entries without downloaded_at use mtime. Capture it
    # before hashing/decoding, and apply only after all output metadata writes.
    os.utime(target, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    if os.name == 'nt':
        _copy_windows_creation_time(source, target)


def commit_replacement(source, staged, name, source_hash, preview_directory, relocate=None):
    """Publish verified bytes before unlinking input; undo index/file on failure."""
    from core import shared
    if source.is_symlink():
        raise ValueError('Source became a symbolic link; original retained')
    original_stat = source.stat()
    version = file_version(source)
    if file_hash(source) != source_hash:
        raise ValueError('Source changed during conversion; original retained')
    metadata = shared.read_image_metadata(str(source))
    target = None
    undo = None
    snapshot = Path(preview_directory) / ('original' + source.suffix)
    try:
        target = install_file(staged, source.parent, name)
        if file_hash(target) != file_hash(staged):
            raise ValueError('Installed file verification failed; original retained')
        if shared.read_image_metadata(str(target)) != metadata:
            raise ValueError('Installed tag/category verification failed; original retained')
        preserve_file_dates(source, target, original_stat)
        with target.open('r+b') as stream:
            os.fsync(stream.fileno())
        if os.name != 'nt':
            descriptor = None
            try:
                descriptor = os.open(source.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
                os.fsync(descriptor)
            except OSError:
                pass
            finally:
                if descriptor is not None:
                    os.close(descriptor)
        # A bounded, temporary comparison snapshot, never an output directory.
        try:
            os.link(source, snapshot)
        except OSError:
            shutil.copy2(source, snapshot)
        if file_hash(snapshot) != source_hash:
            raise ValueError('Comparison snapshot differs; original retained')
        version = file_version(source)
        if relocate:
            undo = relocate(source, target)
        if source.is_symlink() or file_version(source) != version or file_hash(source) != source_hash:
            raise ValueError('Source changed before replacement; original retained')
        source.unlink()
        return target, snapshot
    except BaseException:
        if undo:
            undo()
        if target is not None:
            target.unlink(missing_ok=True)
        snapshot.unlink(missing_ok=True)
        raise


class JxlService:
    def __init__(self, master_folder, changed=None, replaced=None):
        self.master_folder, self.changed, self.replaced = master_folder, changed, replaced
        self.preview_work = None
        self.lock = threading.RLock()
        self.cancel_event = threading.Event()
        self.job = None
        self.thread = None
        self.grants = set()
        atexit.register(self.close)

    def available(self):
        try:
            return importlib.util.find_spec('pillow_jxl') is not None
        except (ImportError, ValueError):
            return False

    def allow_selection(self, path):
        with self.lock:
            if len(self.grants) >= 32:
                self.grants.clear()
            self.grants.add(str(Path(path).resolve()))

    def allowed_path(self, value):
        if not isinstance(value, str) or not value.strip() or '\x00' in value:
            raise ValueError('Choose an image or folder first')
        path = Path(value).expanduser().resolve(strict=True)
        roots = [Path.home().resolve(), Path(self.master_folder()).resolve()]
        with self.lock:
            roots += [Path(p) for p in self.grants]
        if not any(path == root or root in path.parents for root in roots):
            raise ValueError('Choose a path in your home/gallery, or use the desktop picker to select another location')
        return path

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.job) if self.job else {'state': 'idle', 'results': []}

    def start(self, data):
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                raise RuntimeError('A JPEG XL job is already running')
            if not self.available():
                raise ValueError('JPEG XL codec is missing. Install requirements-jxl.txt in the environment running Rems-Dl, then restart.')
            mode = data.get('mode', 'auto')
            if mode not in ('auto', 'lossless', 'lossy', 'restore'):
                raise ValueError('Unknown conversion mode')
            quality, effort = data.get('quality', 90), data.get('effort', 5)
            if type(quality) is not int or not 1 <= quality <= 99 or type(effort) is not int or not 1 <= effort <= 9:
                raise ValueError('Quality must be 1–99 and effort 1–9')
            if type(data.get('recursive', False)) is not bool:
                raise ValueError('Subfolder selection must be true or false')
            source = self.allowed_path(data.get('path'))
            if not (source.is_file() or source.is_dir()):
                raise ValueError('Choose a regular image file or folder')
            self.clear_preview()
            self.cancel_event.clear()
            self.job = {'id': secrets.token_hex(12), 'state': 'scanning', 'mode': mode,
                        'total': 0, 'completed': 0, 'failed': 0, 'current': '',
                        'input_bytes': 0, 'output_bytes': 0, 'results': [], 'error': None,
                        'path': str(source), 'output': str(source if source.is_dir() else source.parent)}
            self.thread = threading.Thread(target=self.run, args=(source, mode, quality, effort, data.get('recursive', False)), name='rems-jxl', daemon=True)
            self.thread.start()
            return copy.deepcopy(self.job)

    def cancel(self):
        self.cancel_event.set()
        with self.lock:
            if self.job and self.job['state'] in ('scanning', 'running'):
                self.job['state'] = 'cancelling'
        return self.snapshot()

    def discover(self, source, mode, recursive):
        extensions = {'.jxl'} if mode == 'restore' else ENCODE_EXTENSIONS
        if source.is_file():
            if source.suffix.lower() not in extensions:
                raise ValueError('Choose a JXL file to restore' if mode == 'restore' else 'Choose a still PNG, JPG/JPEG or WebP image')
            return [source]
        files = []
        for root, dirs, names in os.walk(source, followlinks=False):
            if self.cancel_event.is_set():
                break
            dirs[:] = sorted(d for d in dirs if not d.startswith('.') and d not in ('JXL output', 'Restored images') and not Path(root, d).is_symlink()) if recursive else []
            for name in sorted(names):
                path = Path(root, name)
                if path.suffix.lower() in extensions and not path.is_symlink() and path.is_file():
                    files.append(path)
                    if len(files) > MAX_FILES:
                        raise ValueError('Select a smaller folder: maximum 100,000 images per job')
        return files

    def command(self, request, response):
        if getattr(sys, 'frozen', False):
            return [sys.executable, '--rems-jxl-worker', str(request), str(response)]
        return [sys.executable, str(Path(resource_root()) / 'Rems_Dl.py'), '--rems-jxl-worker', str(request), str(response)]

    def update_active(self, active):
        with self.lock:
            self.job['active'] = [str(item['path']) for item in active]
            self.job['current'] = self.job['active'][0] if active else ''

    def record_result(self, path, result):
        with self.lock:
            row = {'source': str(path), **(result['value'] if result['ok'] else {'error': result['error']})}
            self.job['results'].append(row)
            self.job['results'] = self.job['results'][-100:]
            self.job['completed' if result['ok'] else 'failed'] += 1
            if result['ok']:
                self.job['input_bytes'] += row['input_bytes']
                self.job['output_bytes'] += row['output_bytes']

    def launch(self, path, mode, quality, effort, threads, cost, slot):
        work = tempfile.TemporaryDirectory(prefix='rems-jxl-')
        try:
            directory = Path(work.name)
            request, response = directory / 'request.json', directory / 'response.json'
            request.write_text(json.dumps({'source': str(path), 'mode': mode, 'quality': quality,
                                          'effort': effort, 'threads': threads}), encoding='utf-8')
            process = subprocess.Popen(self.command(request, response), stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, start_new_session=os.name != 'nt',
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            return {'path': path, 'work': work, 'response': response, 'process': process,
                    'started': time.monotonic(), 'cost': cost, 'threads': threads, 'slot': slot}
        except BaseException:
            work.cleanup()
            raise

    def finish(self, item):
        path, process = item['path'], item['process']
        try:
            if process.returncode != 0 or not item['response'].is_file():
                result = {'ok': False, 'error': 'Codec worker stopped or timed out; original kept'}
            else:
                result = json.loads(item['response'].read_text(encoding='utf-8'))
            if result['ok']:
                value = result['value']
                staged = Path(value['output']).resolve()
                if staged.parent != Path(item['work'].name).resolve() or not staged.is_file():
                    raise ValueError('Invalid staged conversion output')
                # Encoding overlaps; final file/index replacement remains serialized.
                with self.lock:
                    if self.cancel_event.is_set():
                        return
                    preview_work = tempfile.TemporaryDirectory(prefix='rems-jxl-compare-', ignore_cleanup_errors=True)
                    try:
                        target, snapshot = commit_replacement(path, staged,
                            value.get('output_name', path.with_suffix('.jxl').name),
                            value['source_sha256'], preview_work.name, self.replaced)
                    except BaseException:
                        preview_work.cleanup()
                        raise
                    self.clear_preview()
                    self.preview_work = preview_work
                    value.update(comparison_source=str(snapshot), comparison_available=True,
                                 replaced=True, output=str(target), token=secrets.token_hex(12))
                    value.pop('source_sha256', None)
            if not self.cancel_event.is_set() or (result['ok'] and result['value'].get('replaced')):
                self.record_result(path, result)
        except Exception as exc:
            if not self.cancel_event.is_set():
                self.record_result(path, {'ok': False, 'error': str(exc)})
        finally:
            item['work'].cleanup()

    @staticmethod
    def stop_all(active):
        # High-bit-depth workers own FFmpeg children. Stop their process trees,
        # so cancelling never leaves a detached encoder using CPU or temp files.
        for item in active:
            process = item['process']
            if os.name != 'nt':
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            elif process.poll() is None:
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), check=False)
                if process.poll() is None:
                    process.terminate()
        deadline = time.monotonic() + 3
        for item in active:
            try:
                item['process'].wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                pass
        for item in active:
            process = item['process']
            if os.name != 'nt':
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif process.poll() is None:
                process.kill()
            process.wait()
            item['work'].cleanup()

    def run(self, source, mode, quality, effort, recursive):
        from core.jxl_cpu import parallel_threads
        from core.jxl_resources import memory_budget, estimate_memory, MIB
        active = []
        try:
            files = self.discover(source, mode, recursive)
            budget = memory_budget()
            allocations = parallel_threads(len(files), budget)
            cpu_total = sum(allocations)
            with self.lock:
                self.job.update(total=len(files), parallel_workers=len(allocations),
                                cpu_threads=cpu_total, memory_budget_bytes=budget, active=[])
                self.job['state'] = 'cancelling' if self.cancel_event.is_set() else 'running'
            if not files and not self.cancel_event.is_set():
                raise ValueError('No supported images found in the selected folder')
            index, waiting = 0, None
            while (index < len(files) or active) and not self.cancel_event.is_set():
                while index < len(files) and len(active) < len(allocations) and not self.cancel_event.is_set():
                    path = files[index]
                    try:
                        if waiting is None:
                            waiting = estimate_memory(path, mode, budget)
                        cost = waiting
                        if cost > budget:
                            raise ValueError(f'Image estimated to need {cost // MIB} MiB exceeds the {budget // MIB} MiB batch memory budget; original kept')
                        # Big rasters, and legacy JXLs with no stored dimensions,
                        # get the whole CPU budget and run alone.
                        exclusive = cost > budget // 2
                        if (exclusive and active) or sum(item['cost'] for item in active) + cost > budget:
                            break
                        used = {item['slot'] for item in active}
                        if any(item['slot'] is None for item in active):
                            break
                        slot = None if exclusive else next(slot for slot in range(len(allocations)) if slot not in used)
                        threads = cpu_total if exclusive else allocations[slot]
                        active.append(self.launch(path, mode, quality, effort, threads, cost, slot))
                        self.update_active(active)
                    except Exception as exc:
                        self.record_result(path, {'ok': False, 'error': str(exc)})
                    index += 1
                    waiting = None
                    if active and active[-1]['slot'] is None:
                        break
                for item in list(active):
                    process = item['process']
                    if process.poll() is None and time.monotonic() - item['started'] > 1800:
                        process.kill()
                        process.wait()
                    if process.poll() is not None:
                        self.finish(item)
                        active.remove(item)
                        self.update_active(active)
                if active:
                    self.cancel_event.wait(.05)
            self.stop_all(active)
            active.clear()
            with self.lock:
                self.job['state'] = 'cancelled' if self.cancel_event.is_set() else 'done'
                self.job['current'], self.job['active'] = '', []
            if self.changed:
                self.changed()
        except Exception as exc:
            with self.lock:
                self.job['state'], self.job['error'] = 'error', str(exc)
                self.job['current'], self.job['active'] = '', []
        finally:
            self.stop_all(active)

    def result(self, token):
        with self.lock:
            return next((dict(r) for r in (self.job or {}).get('results', []) if r.get('token') == token), None)

    def clear_preview(self):
        if self.preview_work is not None:
            self.preview_work.cleanup()
            self.preview_work = None
        for row in (self.job or {}).get('results', []):
            row['comparison_available'] = False
            row.pop('comparison_source', None)

    def close(self):
        self.cancel_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=6)
        if not self.thread or not self.thread.is_alive():
            self.clear_preview()
