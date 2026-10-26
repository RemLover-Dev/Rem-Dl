"""Header-only raster estimates and portable available-memory budgeting."""
import os
from pathlib import Path
import warnings

MAX_PIXELS = 160_000_000
MIB = 1024 * 1024


def available_memory():
    amounts = []
    if os.name == 'nt':
        try:
            import ctypes
            class MemoryStatus(ctypes.Structure):
                _fields_ = [('length', ctypes.c_ulong), ('load', ctypes.c_ulong)] + [(name, ctypes.c_ulonglong) for name in ('total_phys', 'avail_phys', 'total_page', 'avail_page', 'total_virtual', 'avail_virtual', 'extended')]
            status = MemoryStatus()
            status.length = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                amounts.append(status.avail_phys)
        except (AttributeError, OSError):
            pass
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            if line.startswith('MemAvailable:'):
                amounts.append(int(line.split()[1]) * 1024)
    except (OSError, ValueError):
        pass
    if not amounts:
        try:
            amounts.append(os.sysconf('SC_AVPHYS_PAGES') * os.sysconf('SC_PAGE_SIZE'))
        except (AttributeError, OSError, ValueError):
            pass
    try:
        limit = int(Path('/sys/fs/cgroup/memory.max').read_text())
        used = int(Path('/sys/fs/cgroup/memory.current').read_text())
        amounts.append(max(1, limit - used))
    except (OSError, ValueError):
        pass
    return min(value for value in amounts if value > 0) if any(value > 0 for value in amounts) else 2 * 1024 * MIB


def memory_budget():
    return max(64 * MIB, min(8 * 1024 * MIB, available_memory() // 2))


def estimate_memory(path, mode, budget):
    """Estimate native buffers; legacy JXLs without dimensions run alone."""
    path = Path(path)
    size = path.stat().st_size
    if size > 512 * MIB:
        raise ValueError('Choose an image file smaller than 512 MiB')
    highbit = False
    if path.suffix.lower() == '.jxl':
        from core.jxl_metadata import read_record
        record = read_record(path)
        if record is None:
            if mode == 'display':
                return budget
            raise ValueError('This JXL has no Rems conversion record')
        width, height = record.get('width'), record.get('height')
        if type(width) is not int or type(height) is not int:
            return budget
        highbit = record.get('bit_depth') == 16
        pixels = width * height
        reversible = mode != 'display' and record.get('encoding') == 'jpeg'
    else:
        from PIL import Image
        with warnings.catch_warnings():
            # The explicit 160 MP check below runs before any pixel load.
            warnings.simplefilter('ignore', Image.DecompressionBombWarning)
            with Image.open(path) as image:
                if image.format not in ('PNG', 'JPEG', 'WEBP'):
                    raise ValueError('Only still PNG, JPG/JPEG and WebP images are supported')
                if getattr(image, 'n_frames', 1) != 1 or getattr(image, 'is_animated', False):
                    raise ValueError('Animated images are not supported')
                if image.format == 'PNG':
                    from core.jxl_highbit import png_info
                    highbit = (png_info(path) or {}).get('bit_depth') == 16
                pixels = image.width * image.height
                reversible = mode == 'auto' and image.format == 'JPEG'
    if not 0 < pixels <= MAX_PIXELS:
        raise ValueError('Image exceeds the 160 megapixel limit')
    return 128 * MIB + pixels * (64 if highbit else (12 if reversible else 32)) + size * 3
