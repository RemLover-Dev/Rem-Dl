import asyncio

from core.shared import BaseDownloader, STOP_EVENTS


class _StopJoinWorker(BaseDownloader):
    """Scraper dumps more items than the 4 download workers can hold; each
    'download' hangs until stop — mirrors a worker killed mid-transfer."""

    def __init__(self):
        super().__init__("test_stop_join", "Test:StopJoin", 8, {"anti_ban_pause": 0})

    async def _create_session(self):
        self.proxy = None
        return None

    async def scraper_task(self):
        for i in range(8):
            self.download_queue.put_nowait(
                (f"url{i}", f"/tmp/{i}.jpg", f"{i}.jpg", ["t"], [], 0,
                 None, None, None, None, None, None, None))

    async def _async_download_file(self, *item):
        while not self.stop_event.is_set():
            await asyncio.sleep(0.02)
        return False


def test_stop_during_phase2_join_returns():
    dl = _StopJoinWorker()

    async def scenario():
        task = asyncio.create_task(dl.run_async_loop(dl.scraper_task))
        # scraper fills the queue, 4 workers pick up items, 4 stay pending
        await asyncio.sleep(0.3)
        dl.stop_event.set()
        # workers exit at the loop top without task_done() for pending
        # items — run() must still wind down or ACTIVE_JOBS wedges forever
        await asyncio.wait_for(task, 5)

    asyncio.run(scenario())
    # run()'s tail removes its stop event — a leaked one would let a later
    # STOP press kill a fresh run
    assert dl.stop_event not in STOP_EVENTS.get("test_stop_join", [])
