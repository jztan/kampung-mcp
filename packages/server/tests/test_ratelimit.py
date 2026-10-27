import pytest

from conftest import FakeClock
from kampung_server.errors import KampungError
from kampung_server.ratelimit import RateLimiter


def test_rate_limiter_window(clock: FakeClock) -> None:
    rl = RateLimiter(limit=60)
    for _ in range(60):
        rl.check(1, clock.now())
    with pytest.raises(KampungError) as e:
        rl.check(1, clock.now())
    assert e.value.code == "rate_limited"
    rl.check(2, clock.now())
    clock.advance(seconds=61)
    rl.check(1, clock.now())


def test_rejected_sends_do_not_count(clock: FakeClock) -> None:
    rl = RateLimiter(limit=1)
    rl.check(1, clock.now())
    for _ in range(5):
        with pytest.raises(KampungError):
            rl.check(1, clock.now())
    clock.advance(seconds=61)
    rl.check(1, clock.now())
