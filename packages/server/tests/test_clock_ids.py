from datetime import datetime, timedelta, timezone

from kampung_server import ids, limits
from kampung_server.clock import parse_ts, ts, utcnow

T0 = datetime(2026, 10, 27, 9, 0, 0, tzinfo=timezone.utc)


def test_utcnow_is_aware_utc() -> None:
    assert utcnow().tzinfo is not None
    assert utcnow().utcoffset() == timedelta(0)


def test_ts_format() -> None:
    assert ts(T0) == "2026-10-27T09:00:00.000000Z"


def test_ts_sorts_like_time() -> None:
    assert ts(T0 + timedelta(microseconds=1)) > ts(T0)


def test_ts_round_trip() -> None:
    dt = T0 + timedelta(microseconds=123456)
    assert parse_ts(ts(dt)) == dt


def test_new_suffix_two_hex() -> None:
    s = ids.new_suffix(set())
    assert len(s) == 2 and int(s, 16) >= 0


def test_new_suffix_avoids_taken() -> None:
    taken = {f"{i:02x}" for i in range(256)}
    s = ids.new_suffix(taken)
    assert len(s) == 4 and s not in taken


def test_message_ids_sort_by_time() -> None:
    a = ids.new_message_id(T0)
    b = ids.new_message_id(T0 + timedelta(milliseconds=1))
    assert a.startswith("msg_") and len(a) == 4 + 13 + 10
    assert b > a


def test_limits_values() -> None:
    assert limits.MAX_BODY_BYTES == 8192
    assert limits.MESSAGE_TTL == timedelta(days=7)
    assert limits.INBOX_MAX == 20
    assert limits.LIVE_WINDOW == timedelta(minutes=30)
    assert limits.TARGET_FALLBACK == timedelta(hours=24)
    assert limits.WAKE_DEPTH_LIMIT == 3
    assert limits.NOHOOK_DEPTH_RESET == timedelta(minutes=30)
    assert limits.SEND_RATE == 60
    assert limits.SEND_RATE_WINDOW == timedelta(seconds=60)
