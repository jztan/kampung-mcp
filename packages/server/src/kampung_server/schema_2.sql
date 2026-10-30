-- Kampung schema, version 2: indexes for the sweep, which runs inside the
-- write lock before every inbox and peek.

-- Finding sessions to end, and ended sessions to delete.
CREATE INDEX IF NOT EXISTS sessions_live ON sessions (last_seen)
  WHERE ended_at IS NULL;
CREATE INDEX IF NOT EXISTS sessions_ended ON sessions (ended_at)
  WHERE ended_at IS NOT NULL;

-- Finding expired mail.
CREATE INDEX IF NOT EXISTS messages_expiry ON messages (expires_at);

-- Every column that points at a session or a message. Deleting a row makes
-- SQLite look up each reference to it; without these, once per deleted row,
-- that reads the whole messages table.
CREATE INDEX IF NOT EXISTS messages_from ON messages (from_session_id);
CREATE INDEX IF NOT EXISTS messages_to_session ON messages (to_session_id);
CREATE INDEX IF NOT EXISTS messages_delivered_to ON messages (delivered_to);
CREATE INDEX IF NOT EXISTS messages_reply_to ON messages (reply_to);
