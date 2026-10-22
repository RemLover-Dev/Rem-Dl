"""Atomic local state writes; preserve the previous file on serialization failure."""
import json
import os
import stat
import tempfile


def _write(path, writer):
    path = os.path.abspath(path)
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
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
    finally:
        if fd is not None:
            os.close(fd)
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path, data):
    _write(path, lambda stream: json.dump(data, stream))


def write_lines(path, lines):
    _write(path, lambda stream: stream.writelines(lines))
