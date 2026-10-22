"""Synthetic 20k-image/500k-tag search benchmark; isolated temporary database."""
import gc
import os
import statistics
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def before_index(conn):
    index = {}
    for term, gid in conn.execute("SELECT DISTINCT LOWER(REPLACE(tag, '_', ' ')), gallery_id FROM gallery_tags"):
        index.setdefault(term, set()).add(gid)
    return index


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='rems-search-benchmark-') as directory:
        os.environ['REMS_DATA_DIR'] = directory
        os.environ['REMS_HEADLESS'] = '1'
        from core.gallery_store import GalleryStore
        store = GalleryStore()
        entries = [{'id':str(i), 'filename':f'{i}.jpg', 'filepath':f'{i}.jpg', 'site':'danbooru',
                    'downloaded_at':'2026-01-01T00:00:00',
                    'tags':{'tag':[f'tag_{(i*13+j)%25000}' for j in range(25)]}}
                   for i in range(20000)]
        start = time.perf_counter()
        store.save_entries(entries)
        print(f'Dataset + persistent vocabulary creation: {(time.perf_counter()-start)*1000:.1f} ms (one-time, not a query)')
        conn = store._connect()
        start = time.perf_counter()
        index = before_index(conn)
        expected = set().union(*(ids for term, ids in index.items() if 'tag 123' in term))
        print(f'Previous first query/index build: {(time.perf_counter()-start)*1000:.1f} ms')
        del index
        gc.collect()
        tracemalloc.start()
        index = before_index(conn)
        resident = tracemalloc.get_traced_memory()[0]
        tracemalloc.stop()
        print(f'Previous retained Python tag index: {resident/1048576:.1f} MiB')
        del index
        store.close()
        store = GalleryStore()
        start = time.perf_counter()
        assert store.ids_matching_tags('tag 123') == expected
        print(f'Updated first query after reopening persisted index: {(time.perf_counter()-start)*1000:.1f} ms')
        import Rems_Dl as app
        lightweight = {'images':store.lightweight_entries()}
        samples = []
        # Exclude thumbnail generation/filesystem work; this measures metadata
        # search, filtering, paging, page-tag reads and JSON, not visual paint.
        with patch.object(app.shared, '_store', return_value=store), patch.object(app.shared, 'load_gallery', return_value=lightweight), patch.object(app, 'warm_page_thumbs'), patch.dict(app._fp_cache, {'data':None}):
            client = app.app.test_client()
            for _ in range(7):
                start = time.perf_counter()
                response = client.get('/api/gallery?search=tag%20123&per_page=45')
                assert response.status_code == 200 and response.json['total'] == len(expected)
                samples.append((time.perf_counter()-start)*1000)
        print(f'Updated /api/gallery search metadata, median 7: {statistics.median(samples):.1f} ms')
        print('No full Python inverted index retained. Synthetic results; user disk/thumbs/browser paint are not measured.')
        store.close()
