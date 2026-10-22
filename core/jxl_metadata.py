"""Rems metadata in an uncompressed JPEG XL application box, independent of codecs.

Keep the exact category:tag text used by JPEG COM, PNG text and WebP Rems chunks.
The conversion record travels WITH the image, so restore survives app reinstalls.
"""
import json
import os
import tempfile
from pathlib import Path

SIGNATURE = b'\x00\x00\x00\x0cJXL \r\n\x87\n'
BOX = b'rems'
MAX_METADATA = 4 * 1024 * 1024


def boxes(data):
    if not data.startswith(SIGNATURE):
        raise ValueError('A JPEG XL container is required for embedded Rems tags')
    offset = 0
    while offset < len(data):
        if offset + 8 > len(data):
            raise ValueError('Truncated JXL box')
        size = int.from_bytes(data[offset:offset + 4], 'big')
        kind, header = data[offset + 4:offset + 8], 8
        if size == 1:
            if offset + 16 > len(data):
                raise ValueError('Truncated extended JXL box')
            size, header = int.from_bytes(data[offset + 8:offset + 16], 'big'), 16
        if size == 0:
            size = len(data) - offset
        if size < header or offset + size > len(data):
            raise ValueError('Invalid JXL box length')
        yield kind, data[offset + header:offset + size], data[offset:offset + size]
        offset += size


def read_record(path):
    # Streaming reader: scanning metadata must not allocate an entire JXL raster.
    with open(path, 'rb') as stream:
        if stream.read(12) != SIGNATURE:
            return None
        total = os.fstat(stream.fileno()).st_size
        while stream.tell() < total:
            start = stream.tell()
            header = stream.read(8)
            if len(header) != 8:
                raise ValueError('Truncated JXL box')
            size, kind, length = int.from_bytes(header[:4], 'big'), header[4:], 8
            if size == 1:
                extended = stream.read(8)
                if len(extended) != 8:
                    raise ValueError('Truncated extended JXL box')
                size, length = int.from_bytes(extended, 'big'), 16
            if size == 0:
                size = total - start
            if size < length or start + size > total:
                raise ValueError('Invalid JXL box length')
            if kind == BOX:
                if size - length > MAX_METADATA:
                    raise ValueError('Rems tag metadata exceeds 4 MiB')
                record = json.loads(stream.read(size - length).decode('utf-8'))
                if not isinstance(record, dict) or record.get('schema') != 'Rems_Dl/JXL/1':
                    raise ValueError('Unknown Rems JXL metadata schema')
                if record.get('text') is not None and not isinstance(record['text'], str):
                    raise ValueError('Invalid Rems tag text')
                return record
            stream.seek(start + size)
    return None


def write_record(path, record):
    record = dict(record, schema='Rems_Dl/JXL/1')
    payload = json.dumps(record, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if len(payload) > MAX_METADATA:
        raise ValueError('Rems tag metadata exceeds 4 MiB; conversion stopped')
    path = Path(path)
    data = path.read_bytes()
    # Validate all boxes before replacing anything, and put our box before the
    # codestream. No source pixels, JPEG reconstruction data or other boxes change.
    chunks = list(boxes(data))
    prefix = bytearray()
    remainder = bytearray()
    for kind, _body, raw in chunks:
        if kind == BOX:
            continue
        if kind in (b'JXL ', b'ftyp'):
            prefix.extend(raw)
        else:
            remainder.extend(raw)
    encoded = bytes(prefix) + (len(payload) + 8).to_bytes(4, 'big') + BOX + payload + remainder
    fd, temporary = tempfile.mkstemp(prefix='.rems-jxl-meta-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    if read_record(path) != record:
        raise ValueError('JXL metadata verification failed')


def write_text(path, text):
    record = read_record(path) or {}
    record['text'] = text
    write_record(path, record)
