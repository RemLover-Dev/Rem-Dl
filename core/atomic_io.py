"""Atomic local state writes; preserve the previous file on serialization failure."""
import errno
import json
import os
import shutil
import stat
import tempfile


def _write(path, writer):
    path = os.path.abspath(path)
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    # A "clean temp files" utility can delete the freshly created *.tmp in
    # the milliseconds between mkstemp and os.replace — the rename then
    # fails ENOENT and the destination keeps stale content (seen in the
    # wild as a watcher error). One retry with a fresh temp rides that out.
    for attempt in (1, 2):
        fd, temporary = tempfile.mkstemp(prefix='.' + os.path.basename(path) + '.', suffix='.tmp', dir=parent)
        try:
            try:
                mode = stat.S_IMODE(os.stat(path).st_mode)
                if os.path.basename(path) == ".env":
                    mode = 0o600
            except FileNotFoundError:
                mode = 0o600
            os.chmod(temporary, mode)
            with os.fdopen(fd, 'w', encoding='utf-8', newline='') as stream:
                fd = None
                writer(stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            return
        except OSError as e:
            if e.errno != errno.ENOENT or attempt == 2:
                raise
        finally:
            if fd is not None:
                os.close(fd)
            if os.path.exists(temporary):
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass


def write_json(path, data):
    _write(path, lambda stream: json.dump(data, stream))
    _backup(path)


def write_lines(path, lines):
    _write(path, lambda stream: stream.writelines(lines))
    _backup(path)


def _backup(path):
    """Last-known-good copy beside the store, for load paths to recover a
    truncated or wrecked main file from. Cleaners rarely target '.bak'; the
    rename-race row above already covers the targeted '.tmp' pattern."""
    if os.path.basename(path) == ".env":
        return  # secrets keep a single on-disk location
    try:
        shutil.copyfile(path, path + '.bak')
    except OSError:
        pass  # backup is best-effort; the main write already succeeded
