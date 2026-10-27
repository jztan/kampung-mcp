"""Every numeric limit from the design, in one place."""

from datetime import timedelta

MAX_BODY_BYTES = 8192
MESSAGE_TTL = timedelta(days=7)
INBOX_MAX = 20
LIVE_WINDOW = timedelta(minutes=30)
TARGET_FALLBACK = timedelta(hours=24)
WAKE_DEPTH_LIMIT = 3
NOHOOK_DEPTH_RESET = timedelta(minutes=30)
SEND_RATE = 60
SEND_RATE_WINDOW = timedelta(seconds=60)
