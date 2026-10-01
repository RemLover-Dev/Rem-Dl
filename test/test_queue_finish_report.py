"""finish_report: queued runs must report batch totals once, not the last job's."""
import core.shared as shared


def _reset():
    shared._BATCH_STATS.clear()
    shared.queue_has_more = None


def test_single_job_report_matches_legacy(monkeypatch):
    _reset()
    monkeypatch.setattr(shared, "queue_has_more", lambda site: False)
    logs = []
    p = shared.finish_report("gelbooru", 10, 0, 2, False, logs.append)
    assert logs == ["--- All 10 downloads completed successfully! (2 duplicates removed) ---"]
    assert p["jobs"] == 1 and p["downloaded"] == 10 and p["duplicates"] == 2
    assert not p["more"] and not p["stopped"]
    assert "gelbooru" not in shared._BATCH_STATS


def test_batch_aggregates_and_reports_once(monkeypatch):
    _reset()
    pending = [True, True, False]  # jobs 1-2 have successors, job 3 is last
    monkeypatch.setattr(shared, "queue_has_more", lambda site: pending.pop(0))
    logs = []
    p1 = shared.finish_report("gelbooru", 10, 1, 0, False, logs.append)
    assert p1["more"] and p1["jobs"] == 1
    assert logs[0].startswith("--- Tag finished: 10 downloaded, 1 failed")
    p2 = shared.finish_report("gelbooru", 5, 0, 1, False, logs.append)
    assert p2["more"] and p2["jobs"] == 2
    p3 = shared.finish_report("gelbooru", 3, 2, 0, False, logs.append)
    assert not p3["more"] and p3["jobs"] == 3
    assert (p3["downloaded"], p3["failed"], p3["duplicates"]) == (18, 3, 1)
    assert logs[-1] == "--- Queue finished: 3 tasks, 18 downloaded, 3 failed (1 duplicates removed)! ---"
    assert "gelbooru" not in shared._BATCH_STATS


def test_stop_keeps_per_job_wording_and_resets_batch(monkeypatch):
    _reset()
    monkeypatch.setattr(shared, "queue_has_more", lambda site: True)
    logs = []
    shared.finish_report("gelbooru", 10, 0, 0, False, logs.append)  # queued → batch kept
    assert "gelbooru" in shared._BATCH_STATS
    p = shared.finish_report("gelbooru", 4, 1, 0, True, logs.append)  # user stops
    assert p["stopped"] and not p["more"]
    assert logs[-1] == "--- Task finished: 4 downloaded successfully, 1 failed to download! ---"
    assert "gelbooru" not in shared._BATCH_STATS


def test_interim_line_keeps_progress_bars_alive(monkeypatch):
    # updateProgressBar replaces the bars only on these patterns — the interim
    # line must contain none of them, or a queued run's bars die after job 1
    _reset()
    monkeypatch.setattr(shared, "queue_has_more", lambda site: True)
    logs = []
    shared.finish_report("gelbooru", 2, 0, 0, False, logs.append)
    line = logs[0]
    for pat in ("downloads completed successfully", "Task finished", "No new", "No posts", "Queue finished"):
        assert pat not in line, pat
