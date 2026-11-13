"""Every numeric limit from the design, in one place."""

from datetime import timedelta

MAX_BODY_BYTES = 8192
MESSAGE_TTL = timedelta(days=7)
INBOX_MAX = 20
LIVE_WINDOW = timedelta(minutes=30)
# A mailbox with no hooks never sees a typed prompt: its wake budget comes
# back after this long without a waking send instead.
NOHOOK_MAILBOX_REFILL = timedelta(minutes=30)
TARGET_FALLBACK = timedelta(hours=24)
# Owner instructions: unread after this long, one can no longer be read or
# wake anyone (the row keeps MESSAGE_TTL).
OWNER_INSTRUCTION_TTL = timedelta(minutes=60)
# A session that polls (idle wake, Codex) refreshes last_seen every 2 seconds
# (backing off to 30 seconds on failure), so one not seen for this long is gone:
# an owner instruction cannot bind to it, and an idle-wake session reports
# reachable false (its machine is probably asleep or offline).
POLL_WINDOW = timedelta(minutes=2)
OWNER_BIND_WINDOW = POLL_WINDOW
# A Codex thread is bound only if someone typed in it this recently: a closed
# thread stays loaded and wakeable.
OWNER_CODEX_TYPED_WINDOW = timedelta(minutes=60)
SEND_RATE = 60
SEND_RATE_WINDOW = timedelta(seconds=60)
REGISTER_RATE = 60
# Typed-prompt calls per machine per minute: each refills wake budgets.
HUMAN_TURN_RATE = 60
STREAM_CONNECT_RATE = 120  # event stream connects per machine per minute
SUMMARY_RATE = 30  # set_summary calls per machine per minute
# peek calls per machine per minute: the Channels poller peeks every 2
# seconds, so this covers MAX_LIVE_SESSIONS_PER_MACHINE windows polling at once.
PEEK_RATE = 256 * 30
# end calls per machine per minute, apart from peek so polling never blocks one.
END_RATE = 600
# The audit log keeps 90 days and at most this many rows, pruned a batch per
# sweep so a backlog never holds the write lock for long.
AUDIT_RETENTION = timedelta(days=90)
AUDIT_MAX_ROWS = 500_000
AUDIT_DELETE_BATCH = 1000
SWEEP_EVERY = timedelta(minutes=5)
MAX_REQUEST_BYTES = 64 * 1024
MAX_CWD_CHARS = 1024
MAX_BRANCH_CHARS = 100
MAX_CLIENT_VERSION_CHARS = 32
MAX_SUMMARY_CHARS = 80
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
# Live sign-ins per client address; a fourth drops that address's oldest, so
# one address cannot fill OAUTH_MAX_REQUESTS and lock the owner out.
OAUTH_REQUESTS_PER_CLIENT = 3
OAUTH_CONNECT_ATTEMPTS = 5
CONNECT_FAILURE_LIMIT = 20  # wrong codes per client address
CONNECT_FAILURE_WINDOW = timedelta(minutes=15)
MAX_OAUTH_BODY_BYTES = 64 * 1024
CONNECTOR_WAKE_WINDOW = timedelta(minutes=10)
# Attachments: one per message, stored apart from the body.
MAX_ATTACHMENT_TEXT_BYTES = 256 * 1024
MAX_ATTACHMENT_IMAGE_BYTES = 512 * 1024
IMAGE_LONG_EDGE = 1568  # what Claude keeps of an image anyway
# Checked from the header, before decoding: a decoded RGBA image takes 4 bytes
# a pixel, so this holds one decode to about 32 MB. Only IMAGE_DECODES images
# hold full-size pixels at once: each is shrunk before its slot is released.
# Our adapter shrinks images to IMAGE_LONG_EDGE before sending.
IMAGE_MAX_PIXELS = 8_000_000
IMAGE_DECODES = 2
# Seconds a send waits for a decode slot before it is refused as busy, so a
# burst of images fails fast instead of holding request threads.
IMAGE_DECODE_WAIT = 5.0
# Claude Code moves a text result over 50,000 characters into a file the
# Claude app cannot open, so reads come in slices well under that.
READ_SLICE_DEFAULT = 16 * 1024
READ_SLICE_MAX = 32 * 1024
MAX_UNREAD_ATTACHMENT_BYTES_PER_MACHINE = 25 * 1024 * 1024
MAX_ATTACHMENT_BYTES_TOTAL = 500 * 1024 * 1024
MIN_FREE_DISK_BYTES = 1024**3
MAX_MCP_BODY_BYTES = 1024 * 1024
