"""Elapsed-time estimate for a single JPEG XL encode.

The encoder is one blocking native call with no intra-encode callback, so the
only per-image percentage available is an inference: a measured seconds-per-
pixel rate turns elapsed time into a fraction of the current file. Rates are
learned per (kind, effort) after the first completed file of that shape.
"""
from pathlib import Path
import threading

# shortcut: bootstrap rates (s/MP at effort 5) until the first real sample
# replaces them — only the first file of a fresh kind/effort is approximate.
_SEED = {'jpeg': 0.015, 'lossless': 0.09, 'highbit': 0.30}
_RATE = {}
_LOCK = threading.Lock()
MAX_FRACTION = 0.99


def kind_of(path):
    suffix = Path(path).suffix.lower()
    if suffix in ('.jpg', '.jpeg'):
        return 'jpeg'
    if suffix == '.png':
        try:
            from core.jxl_highbit import png_info
            if (png_info(path) or {}).get('bit_depth') == 16:
                return 'highbit'
        except OSError:
            pass
    return 'lossless'


def _key(kind, effort):
    return (kind, int(effort or 5))


def eta_seconds(pixels, kind, effort=5):
    """Expected encode seconds for this image; 0 when the size is unknown."""
    if pixels <= 0:
        return 0.0
    seed = _SEED.get(kind, 0.1) * (max(1, int(effort or 5)) / 5) ** 1.5
    with _LOCK:
        rate = _RATE.get(_key(kind, effort), seed)
    return pixels / 1_000_000 * rate


def observe(kind, effort, seconds, pixels):
    """Feed one completed encode back into the rate estimate."""
    if pixels <= 0 or seconds <= 0:
        return
    sample = seconds / (pixels / 1_000_000)
    with _LOCK:
        key = _key(kind, effort)
        _RATE[key] = sample if key not in _RATE else 0.7 * _RATE[key] + 0.3 * sample


def fraction(started, eta, now):
    """Fraction of the current file done, capped so it never reads complete."""
    if not eta or eta <= 0:
        return 0.0
    return max(0.0, min(MAX_FRACTION, (now - started) / eta))
