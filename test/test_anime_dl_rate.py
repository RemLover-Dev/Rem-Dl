import time

from workers.anime_dl import _RateLimiter, _rate_limit_session


def test_rate_limiter_spaces_requests_at_four_per_second():
    rl = _RateLimiter(4)
    rl.wait()  # first slot is immediate
    t0 = time.monotonic()
    for _ in range(3):
        rl.wait()
    # three reserved slots at 0.25s each; slack for scheduler jitter
    assert time.monotonic() - t0 >= 0.7


def test_session_get_passes_through_limiter():
    class _S:
        def get(self, url, **kw):
            return ("ok", url)

    seen = []

    class _Rate:
        def wait(self):
            seen.append(1)

    s = _rate_limit_session(_S(), _Rate())
    assert s.get("u") == ("ok", "u")
    assert seen == [1]
