from core import jxl_progress as progress


def test_fraction_tracks_elapsed_and_never_reads_complete():
    assert progress.fraction(100.0, 0, 105.0) == 0.0
    assert progress.fraction(100.0, 10.0, 105.0) == 0.5
    assert progress.fraction(100.0, 10.0, 90.0) == 0.0
    assert progress.fraction(100.0, 10.0, 200.0) == progress.MAX_FRACTION


def test_eta_learns_from_observed_encodes():
    kind, effort, pixels = 'lossless', 9, 2_000_000
    assert progress.eta_seconds(pixels, kind, effort) > 0
    for _ in range(20):
        progress.observe(kind, effort, 4.0, pixels)
    assert abs(progress.eta_seconds(pixels, kind, effort) - 4.0) < 0.05


def test_kind_and_unknown_size(tmp_path):
    assert progress.kind_of(tmp_path / 'a.JPG') == 'jpeg'
    assert progress.kind_of(tmp_path / 'a.webp') == 'lossless'
    assert progress.eta_seconds(0, 'lossless', 5) == 0.0


def test_tick_reports_a_fraction_inside_the_current_file():
    import time
    from core import shared
    events = []
    real_emit = shared.emit_callback
    shared.emit_callback = lambda ev, data: events.append(data) if ev == 'conversion_progress' else None
    try:
        shared._PENDING_PUBLISH['/x.png'] = {'site': 's', 'filename': 'x.png'}
        shared._CONVERT_STATE['s'] = {'total': 1, 'payload': {}}
        shared._CONVERT_CURRENT['s'] = 'x.png'
        shared._CONVERT_ETA['s'] = {'file': 'x.png', 'started': time.monotonic() - 5, 'eta': 10}
        shared.notify_convert_tick('/x.png')
        assert events and 0 < events[-1]['done'] < 1
    finally:
        shared.emit_callback = real_emit
        shared._PENDING_PUBLISH.clear()
        shared._CONVERT_STATE.clear()
        shared._CONVERT_CURRENT.clear()
        shared._CONVERT_ETA.clear()
