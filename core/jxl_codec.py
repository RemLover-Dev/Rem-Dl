"""Local JPEG XL conversions. Originals are never overwritten."""
from __future__ import annotations

import io
import math
import os
import shutil
import tempfile
import time
import warnings
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace

from PIL import Image, ImageChops, ImageCms, ImageOps
import pillow_jxl
from pillow_jxl import JpegXLImagePlugin
from core.jxl_cpu import MAX_THREADS
from core.jxl_resources import MAX_PIXELS

THREADS = max(1, min(2, os.cpu_count() or 1))
JpegXLImagePlugin.DECODE_THREADS = THREADS
MAX_FILE = 512 * 1024 * 1024
MODES = ('lossless', 'lossy', 'jpeg', 'png', 'restore')
SRGB = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()


@dataclass(frozen=True)
class Options:
    mode: str = 'lossless'
    quality: int = 90
    effort: int = 5
    threads: int = THREADS

    def validate(self):
        if self.mode not in MODES:
            raise ValueError('Unknown conversion mode')
        if not 1 <= self.quality <= 99:
            raise ValueError('Lossy quality must be between 1 and 99')
        if not 1 <= self.effort <= 9:
            raise ValueError('Effort must be between 1 and 9')
        if type(self.threads) is not int or not 1 <= self.threads <= MAX_THREADS:
            raise ValueError(f'Threads must be between 1 and {MAX_THREADS}')


def check_file(path):
    path = Path(path).expanduser().resolve(strict=True)
    if not path.is_file() or path.stat().st_size > MAX_FILE:
        raise ValueError('Choose an image file smaller than 512 MiB')
    return path


def open_still(path, decode=True):
    path = check_file(path)
    # Pillow downconverts 16-bit RGB PNG to 8 bits, so reject it before opening.
    with path.open('rb') as stream:
        header = stream.read(32)
    if header.startswith(b'\x89PNG\r\n\x1a\n') and len(header) > 24 and header[24] > 8:
        raise ValueError('This test tool supports 8-bit SDR images. 16-bit PNG needs a high-bit-depth tool.')
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', Image.DecompressionBombWarning)
        with Image.open(path) as image:
            if image.format not in ('PNG', 'JPEG', 'WEBP', 'JXL'):
                raise ValueError('Only still PNG, JPG/JPEG and WebP images are supported; GIFs and videos are excluded')
            if image.width * image.height > MAX_PIXELS:
                raise ValueError('Image exceeds the 160 megapixel limit')
            if getattr(image, 'n_frames', 1) != 1 or getattr(image, 'is_animated', False):
                raise ValueError('Animated and multi-page inputs are not supported')
            if image.mode in ('I', 'F') or image.mode.startswith('I;16'):
                raise ValueError('This test tool supports 8-bit SDR images, not HDR or high-bit-depth inputs')
            bits = getattr(image, 'tag_v2', {}).get(258, (8,))
            if isinstance(bits, int):
                bits = (bits,)
            if max(bits) > 8:
                raise ValueError('High-bit-depth TIFF is not supported; no 8-bit downconversion was performed')
            if not decode:
                return SimpleNamespace(format=image.format, size=image.size, width=image.width, height=image.height)
            image.load()
            result = image.copy()
            result.info = dict(image.info)
            result.format = image.format
            return result


def normalize(path):
    """Upright 8-bit sRGB pixels; metadata other than the sRGB profile is removed."""
    from core import jxl_highbit
    image = ImageOps.exif_transpose(jxl_highbit.display_image(path) if jxl_highbit.is_highbit(path)
                                    else open_still(path))
    profile = image.info.get('icc_profile')
    has_alpha = image.mode in ('RGBA', 'LA', 'PA') or 'transparency' in image.info
    alpha = image.convert('RGBA').getchannel('A') if has_alpha else None
    if profile:
        try:
            inp = image if image.mode in ('RGB', 'L', 'CMYK') else image.convert('RGB')
            image = ImageCms.profileToProfile(inp, ImageCms.ImageCmsProfile(io.BytesIO(profile)),
                                             ImageCms.createProfile('sRGB'), outputMode='RGB')
        except Exception as exc:
            raise ValueError('Cannot interpret this ICC profile safely; conversion was stopped') from exc
    elif image.mode not in ('L', 'LA', 'RGB', 'RGBA'):
        image = image.convert('RGBA' if has_alpha else 'RGB')
    if alpha is not None and image.mode not in ('RGBA', 'LA'):
        image.putalpha(alpha)
    # A new image prevents EXIF/XMP and JPEG auto-recompression from leaking in.
    clean = Image.frombytes(image.mode, image.size, image.tobytes())
    clean.info['icc_profile'] = SRGB
    return clean


def compare_pixels(original, decoded):
    if original.size != decoded.size:
        raise ValueError('Verification failed: decoded dimensions differ')
    a, b = original.convert('RGBA'), decoded.convert('RGBA')
    hist = ImageChops.difference(a, b).histogram()
    squared = sum((i % 256) ** 2 * count for i, count in enumerate(hist))
    count = a.width * a.height * 4
    mse = squared / count
    return {'exact': squared == 0, 'rmse': math.sqrt(mse),
            'psnr_db': None if mse == 0 else 10 * math.log10(255 * 255 / mse)}


def suffix(options):
    return {'lossless': '-lossless.jxl', 'lossy': f'-q{options.quality}-e{options.effort}.jxl',
            'jpeg': '-jpeg-repack.jxl', 'png': '-decoded.png', 'restore': '-restored.jpg'}[options.mode]


def publish(staged, directory, name):
    """Install without replacing any existing file, including during name races."""
    directory = Path(directory).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    stem, ext = os.path.splitext(name)
    for number in range(10000):
        target = directory / (name if number == 0 else f'{stem} ({number}){ext}')
        try:
            # Atomic no-clobber publication on filesystems supporting hard links.
            os.link(staged, target)
            return target
        except FileExistsError:
            continue
        except OSError:
            # Cross-filesystem/FAT fallback; exclusive creation still protects originals.
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
    raise ValueError('Too many files with this output name')


def convert(source, output_directory, options=Options()):
    options.validate()
    source = check_file(source)
    started = time.monotonic()
    original_size = source.stat().st_size
    metrics = None
    if options.mode in ('png', 'restore'):
        if source.suffix.lower() != '.jxl':
            raise ValueError('Choose a .jxl file for this operation')
    if options.mode == 'restore':
        jpeg, info, payload, _icc, _boxes = pillow_jxl.Decoder(num_threads=options.threads)(source.read_bytes())
        if not jpeg:
            raise ValueError('This JXL has no reversible JPEG data. Use JXL → PNG instead.')
        if not payload.startswith(b'\xff\xd8'):
            raise ValueError('Invalid JPEG reconstruction')
        note = 'Original JPEG bitstream restored; its original metadata is retained.'
        size = (info.width, info.height)
    elif options.mode == 'png':
        image = open_still(source)
        size = image.size
        buffer = io.BytesIO()
        clean = Image.frombytes(image.mode, image.size, image.tobytes())
        profile = image.info.get('icc_profile')
        clean.save(buffer, format='PNG', icc_profile=profile)
        payload = buffer.getvalue()
        note = 'Decoded pixels and ICC profile exported to PNG; other metadata is removed.'
    elif options.mode == 'jpeg':
        image = open_still(source, decode=False)
        if image.format != 'JPEG':
            raise ValueError('Reversible JPEG mode requires an original .jpg/.jpeg file')
        raw = source.read_bytes()
        encoder = pillow_jxl.Encoder(mode='RGB', lossless=True, effort=options.effort,
                                     use_container=True, num_threads=options.threads)
        payload = encoder(raw, image.width, image.height, jpeg_encode=True)
        reconstructed = pillow_jxl.Decoder(num_threads=options.threads)(payload)
        if not reconstructed[0] or reconstructed[2] != raw:
            raise ValueError('Verification failed: original JPEG bytes did not round-trip')
        size = image.size
        metrics = {'exact': True, 'rmse': 0, 'psnr_db': None}
        note = 'Verified byte-for-byte JPEG reconstruction. Original metadata is retained.'
    else:
        image = normalize(source)
        size = image.size
        buffer = io.BytesIO()
        image.save(buffer, format='JXL', lossless=options.mode == 'lossless',
                   quality=options.quality, effort=options.effort, lossless_jpeg=False,
                   use_original_profile=options.mode == 'lossless', use_container=True,
                   num_threads=options.threads, exif=b'', xmp=b'')
        payload = buffer.getvalue()
        with Image.open(io.BytesIO(payload)) as decoded:
            decoded.load()
            metrics = compare_pixels(image, decoded)
        if options.mode == 'lossless' and not metrics['exact']:
            raise ValueError('Lossless verification failed; no output was installed')
        note = ('Verified identical normalized pixels.' if options.mode == 'lossless'
                else 'Lossy encoding: lower quality usually makes smaller files.')
        note += ' EXIF orientation is applied, ICC colors converted to sRGB, private metadata removed.'
    directory = Path(output_directory).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    # The GUI gives each subprocess a private staging directory, enabling safe cancellation.
    with tempfile.TemporaryDirectory(prefix='.jxl-lab-', dir=directory) as work:
        staged = Path(work) / 'encoded'
        staged.write_bytes(payload)
        target = publish(staged, directory, source.stem + suffix(options))
    return {'source': str(source), 'output': str(target), 'mode': options.mode,
            'input_bytes': original_size, 'output_bytes': len(payload),
            'saving_percent': 100 * (1 - len(payload) / max(original_size, 1)),
            'seconds': time.monotonic() - started, 'width': size[0], 'height': size[1],
            'metrics': metrics, 'note': note, 'options': asdict(options)}

