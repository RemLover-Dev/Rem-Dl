"""Viewer display decode: thread budget + native-JXL byte mapping."""
import io
import os

import pytest

import Rems_Dl


def test_viewer_thread_budget():
    # half the cores, capped at 4 — the pool's worst case must fit in cores
    n = Rems_Dl._JXL_DISPLAY_DECODE_THREADS
    assert n == max(1, min(4, (os.cpu_count() or 4) // 2))
    assert n * 2 <= (os.cpu_count() or 4)  # two display workers + inline decode


def test_native_jxl_rgba_same_pixels_as_plugin():
    pytest.importorskip("pillow_jxl")
    import pillow_jxl
    from PIL import Image, ImageChops
    from pillow_jxl import JpegXLImagePlugin

    JpegXLImagePlugin.DECODE_THREADS = 1  # reference decode stays single-threaded
    im = Image.new("RGBA", (33, 17))     # non-multiple-of-8 edges lock stride handling
    for y in (0, 1, 8, 16):
        for x in range(33):
            im.putpixel((x, y), (x * 7 % 256, y * 15 % 256, 60,
                                 (x + y) % 256))
    enc = pillow_jxl.Encoder(mode="RGBA", lossless=True, effort=3,
                             use_original_profile=True, num_threads=1)
    payload = enc(im.tobytes(), im.width, im.height, jpeg_encode=False)

    ref = Image.open(io.BytesIO(payload)).convert("RGBA")
    got = Rems_Dl._jxl_display_image(payload, 4096)  # under the cap: no draft resize
    assert got.mode == ref.mode and got.size == ref.size
    assert ImageChops.difference(ref, got).getbbox() is None
