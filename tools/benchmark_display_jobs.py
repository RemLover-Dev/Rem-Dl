"""Reproducible synthetic job-registry benchmark, not an end-to-end UI test."""
import ast
import concurrent.futures
import statistics
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.jobs import submit_shared


def run(source, modern):
    tree = ast.parse(source)
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_generate_display_full')
    namespace = {'submit_shared': submit_shared}
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as workers:
        namespace.update(_display_pool=workers, _display_inflight={},
                         _display_inflight_lock=threading.RLock() if modern else threading.Lock())
        def work(key):
            time.sleep(0.03)
            return key
        namespace['_make_display_copy'] = work
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<benchmark>', 'exec'), namespace)
        started = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as callers:
            result = list(callers.map(namespace['_generate_display_full'], [str(i) for i in range(8)]))
        assert result == [str(i) for i in range(8)]
        return time.perf_counter() - started


if __name__ == '__main__':
    current = (Path(__file__).resolve().parents[1] / 'Rems_Dl.py').read_text()
    # Minimal expression of the previous wait-under-lock implementation.
    baseline = '''def _generate_display_full(full):
    with _display_inflight_lock:
        future = _display_inflight.get(full)
        if future is None:
            future = _display_pool.submit(_make_display_copy, full)
            _display_inflight[full] = future
            future.add_done_callback(lambda done, key=full: _display_inflight.pop(key, None))
        return future.result()
'''
    before = statistics.median(run(baseline, False) for _ in range(3))
    after = statistics.median(run(current, True) for _ in range(3))
    print(f'8 distinct jobs, 2 workers, 30 ms/job: baseline={before*1000:.1f} ms updated={after*1000:.1f} ms ratio={before/after:.2f}x')
    print('Synthetic concurrency benchmark; no claim about whole-app speed or GPU rendering.')
