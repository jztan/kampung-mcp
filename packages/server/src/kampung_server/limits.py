"""Every numeric limit from the design, in one place."""

from datetime import timedelta

MAX_BODY_BYTES = 8192
MESSAGE_TTL = timedelta(days=7)
INBOX_MAX = 20
LIVE_WINDOW = timedelta(minutes=30)
TARGET_FALLBACK = timedelta(hours=24)
SEND_RATE = 60
SEND_RATE_WINDOW = timedelta(seconds=60)
REGISTER_RATE = 60
SWEEP_EVERY = timedelta(minutes=5)
MAX_REQUEST_BYTES = 64 * 1024
MAX_CWD_CHARS = 1024
MAX_AGENT_SESSION_ID_CHARS = 128
MAX_LIVE_SESSIONS_PER_MAILBOX = 32
MAX_LIVE_SESSIONS_PER_MACHINE = 256
MAX_MAILBOXES_PER_MACHINE = 1000
MAX_WAITING_PER_MAILBOX = 1000
ENDED_SESSION_RETENTION = timedelta(days=30)
SESSION_DELETE_BATCH = 1000
MAX_WAITING_PER_SENDER = 200
AUTH_FAILURE_LIMIT = 20
AUTH_FAILURE_WINDOW = timedelta(seconds=60)
# Claude app connector.
OAUTH_CONNECT_CODE_TTL = timedelta(minutes=10)
OAUTH_REQUEST_TTL = timedelta(minutes=10)
OAUTH_CODE_TTL = timedelta(minutes=5)
OAUTH_ACCESS_TTL = timedelta(hours=1)
OAUTH_REFRESH_TTL = timedelta(days=90)
OAUTH_REFRESH_REUSE = timedelta(seconds=60)
# How long an expired access token and a rotated refresh token stay on record:
# the first so a stale token gets a 401 and a refresh rather than a throttled
# 429, the second so its reuse still ends the grant.
OAUTH_ROTATED_RETENTION = timedelta(days=7)
OAUTH_MAX_REQUESTS = 100
OAUTH_CONNECT_ATTEMPTS = 5
CONNECT_FAILURE_LIMIT = 20  # wrong codes per client address
CONNECT_FAILURE_WINDOW = timedelta(minutes=15)
MAX_OAUTH_BODY_BYTES = 64 * 1024
CONNECTOR_WAKE_BUDGET = 10
CONNECTOR_WAKE_WINDOW = timedelta(minutes=10)
# Attachments: one per message, stored apart from the body.
MAX_ATTACHMENT_TEXT_BYTES = 256 * 1024
MAX_ATTACHMENT_IMAGE_BYTES = 512 * 1024
IMAGE_LONG_EDGE = 1568  # what Claude keeps of an image anyway
IMAGE_MAX_PIXELS = 25_000_000  # checked from the header, before decoding
# Claude Code moves a text result over 50,000 characters into a file the
# Claude app cannot open, so reads come in slices well under that.
READ_SLICE_DEFAULT = 16 * 1024
READ_SLICE_MAX = 32 * 1024
MAX_UNREAD_ATTACHMENT_BYTES_PER_MACHINE = 25 * 1024 * 1024
MAX_ATTACHMENT_BYTES_TOTAL = 500 * 1024 * 1024
MIN_FREE_DISK_BYTES = 1024**3
MAX_MCP_BODY_BYTES = 1024 * 1024
