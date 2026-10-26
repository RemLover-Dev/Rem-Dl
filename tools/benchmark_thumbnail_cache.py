"""Cold vs warm thumbnail-cache microbenchmark using isolated temporary data."""
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='rems-thumb-benchmark-') as directory:
        os.environ['REMS_DATA_DIR'] = directory
        os.environ['REMS_HEADLESS'] = '1'
        import Rems_Dl as main
        from PIL import Image
        sources = []
        for i in range(8):
            path = Path(directory) / f'{i}.png'
            Image.new('RGBA', (1800, 2400), (i * 20, 30, 40, 180)).save(path)
            sources.append(str(path))
        started = time.perf_counter()
        for path in sources:
            main._cached_thumb(path)
        cold = time.perf_counter() - started
        samples = []
        for _ in range(5):
            started = time.perf_counter()
            for path in sources:
                main._cached_thumb(path)
            samples.append(time.perf_counter() - started)
        warm = statistics.median(samples)
        print(f'8 synthetic 1800x2400 RGBA PNGs: cold={cold*1000:.1f} ms warm={warm*1000:.1f} ms')
        print('The old warm path always decoded/rebuilt every thumbnail. Updated warm hits do zero full-image decodes.')
        print('Microbenchmark only; not an end-to-end application speed claim.')
