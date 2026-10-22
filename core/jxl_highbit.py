"""16-bit PNG/JXL path using FFmpeg's native 16-bit libpng/libjxl codecs.

Pillow is used only for header metadata and 8-bit display copies. It never
encodes the high-bit-depth master. No extra Python imaging dependency is used.
"""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

from core.jxl_resources import MAX_PIXELS

SIGNATURE = b'\x89PNG\r\n\x1a\n'
FORMATS = {0: 'gray16be', 2: 'rgb48be', 4: 'ya16be', 6: 'rgba64be'}


def png_info(path):
    with open(path, 'rb') as stream:
        header = stream.read(33)
    if not header.startswith(SIGNATURE) or header[12:16] != b'IHDR' or len(header) != 33:
        return None
    return {'width': int.from_bytes(header[16:20], 'big'),
            'height': int.from_bytes(header[20:24], 'big'),
            'bit_depth': header[24], 'color_type': header[25]}


def is_highbit(path):
    path = Path(path)
    if path.suffix.lower() == '.png':
        return (png_info(path) or {}).get('bit_depth') == 16
    if path.suffix.lower() == '.jxl':
        from core.jxl_metadata import read_record
        return (read_record(path) or {}).get('bit_depth') == 16
    return False


def _command(source, target, threads, output_options):
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        raise ValueError('16-bit conversion requires FFmpeg with libjxl support; original kept')
    return [ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
            '-threads', str(threads), '-i', str(Path(source).resolve()),
            '-map', '0:v:0', '-frames:v', '1', '-threads', str(threads),
            '-filter_threads', '1', *output_options, str(target)]


def _run(source, target, threads, options):
    result = subprocess.run(_command(source, target, threads, options),
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode or not Path(target).is_file():
        detail = result.stderr.decode('utf-8', 'replace')[-1000:]
        raise ValueError('16-bit codec failed (FFmpeg needs libjxl support): ' + detail)


def pixel_hash(source, width, height, threads):
    """Hash every sample as canonical 16-bit RGBA, streaming without a big buffer."""
    expected = width * height * 8
    digest, count = hashlib.sha256(), 0
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(_command(source, 'pipe:1', threads,
            ['-f', 'rawvideo', '-pix_fmt', 'rgba64be']), stdout=subprocess.PIPE,
            stderr=errors, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            while True:
                block = process.stdout.read(1024 * 1024)
                if not block:
                    break
                count += len(block)
                if count > expected:
                    raise ValueError('Decoded image exceeds expected dimensions')
                digest.update(block)
            if process.wait() or count != expected:
                raise ValueError('Could not verify all 16-bit pixel samples')
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.kill()
            process.wait()
    return digest.hexdigest()


def icc_hash(path):
    from PIL import Image
    with Image.open(path) as image:
        profile = image.info.get('icc_profile')
    return hashlib.sha256(profile).hexdigest() if profile else None


def convert(source, directory, options):
    started = time.monotonic()
    info = png_info(source)
    if not info or info['bit_depth'] != 16 or info['color_type'] not in FORMATS:
        raise ValueError('Expected a 16-bit still PNG')
    width, height = info['width'], info['height']
    if not 0 < width * height <= MAX_PIXELS:
        raise ValueError('Image exceeds the 160 megapixel limit')
    from PIL import Image
    with Image.open(source) as header:
        if getattr(header, 'n_frames', 1) != 1 or getattr(header, 'is_animated', False):
            raise ValueError('Animated images are not supported')
        # Keep the original coordinate system; unsupported EXIF rotations must
        # never be mistaken for a successful exact pixel conversion.
        if header.getexif().get(0x0112, 1) != 1:
            raise ValueError('16-bit PNG with EXIF rotation is not supported; original kept')
        transparency = 'transparency' in header.info
        if not header.info.get('icc_profile') and 'srgb' not in header.info:
            gamma = header.info.get('gamma', .45455)
            chroma = header.info.get('chromaticity', (.3127,.329,.64,.33,.3,.6,.15,.06))
            srgb_chroma = (.3127,.329,.64,.33,.3,.6,.15,.06)
            if abs(gamma - .45455) > .00002 or any(abs(a-b) > .00002 for a,b in zip(chroma,srgb_chroma)):
                raise ValueError('16-bit PNG with custom gamma/primaries needs an ICC profile; original kept')
    if options.mode not in ('lossless', 'lossy'):
        raise ValueError('16-bit PNG requires lossless or lossy JXL mode')
    directory = Path(directory)
    target, decoded = directory / 'encoded16.jxl', directory / 'verified16.png'
    pixel_format = 'rgba64be' if transparency else FORMATS[info['color_type']]
    before = pixel_hash(source, width, height, options.threads)
    distance = 0 if options.mode == 'lossless' else .1 + (100 - options.quality) * .09
    _run(source, target, options.threads,
         ['-c:v', 'libjxl', '-pix_fmt', pixel_format.replace('be', 'le'),
          '-distance', str(distance), '-effort', str(options.effort)])
    try:
        _run(target, decoded, options.threads, ['-c:v', 'png', '-pix_fmt', pixel_format])
        decoded_info = png_info(decoded) or {}
        if (decoded_info.get('bit_depth'), decoded_info.get('width'), decoded_info.get('height')) != (16, width, height):
            raise ValueError('Codec reduced bit depth; original kept')
        after = pixel_hash(decoded, width, height, options.threads)
        exact = before == after
        if options.mode == 'lossless' and not exact:
            raise ValueError('16-bit lossless pixel verification failed; original kept')
        profile = icc_hash(source)
        if profile and icc_hash(decoded) != profile:
            raise ValueError('16-bit ICC profile verification failed; original kept')
    finally:
        decoded.unlink(missing_ok=True)
    size = Path(source).stat().st_size
    return {'source': str(source), 'output': str(target), 'mode': options.mode,
            'input_bytes': size, 'output_bytes': target.stat().st_size,
            'saving_percent': 100 * (1 - target.stat().st_size / max(size, 1)),
            'width': width, 'height': height, 'seconds': time.monotonic() - started,
            'metrics': {'exact': exact},
            'highbit': {'bit_depth': 16, 'pixel_format': pixel_format,
                        'pixel_sha256': after, 'icc_sha256': profile},
            'note': '16-bit pixels and ICC preserved; all channel samples verified.' if exact
                    else 'Lossy 16-bit JXL; ICC preserved. Removed detail cannot be recovered.'}


def restore(source, directory, record, threads):
    width, height = record.get('width'), record.get('height')
    if type(width) is not int or type(height) is not int or not 0 < width * height <= MAX_PIXELS:
        raise ValueError('Invalid 16-bit image dimensions in conversion record')
    pixel_format = record.get('pixel_format')
    if pixel_format not in FORMATS.values():
        raise ValueError('Invalid 16-bit PNG pixel format')
    target = Path(directory) / 'restored16.png'
    _run(source, target, threads, ['-c:v', 'png', '-pix_fmt', pixel_format])
    restored_info = png_info(target) or {}
    if (restored_info.get('bit_depth'), restored_info.get('width'), restored_info.get('height')) != (16, width, height):
        raise ValueError('Restored PNG lost bit depth; original kept')
    if pixel_hash(target, width, height, threads) != record.get('pixel_sha256'):
        raise ValueError('Restored 16-bit sample verification failed; original kept')
    if record.get('icc_sha256') and icc_hash(target) != record['icc_sha256']:
        raise ValueError('Restored ICC profile verification failed; original kept')
    return {'source': str(source), 'output': str(target), 'input_bytes': Path(source).stat().st_size,
            'output_bytes': target.stat().st_size, 'width': width, 'height': height,
            'metrics': {'exact': True}, 'note': '16-bit PNG restored; all decoded samples and ICC verified.'}


def display_image(source):
    """An 8-bit copy for browsers only; never modifies the high-bit-depth master."""
    from PIL import Image
    from core.jxl_codec import THREADS
    with tempfile.TemporaryDirectory(prefix='rems-16-display-') as d:
        target = Path(d) / 'display.png'
        _run(source, target, THREADS, ['-c:v', 'png', '-pix_fmt', 'rgba'])
        with Image.open(target) as image:
            image.load()
            result = image.copy()
            result.info = dict(image.info)
            return result
