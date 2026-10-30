-- Kampung schema, version 1. sessions.last_delivered_at drives the no-hooks
-- depth reset.

CREATE TABLE machines (
  id           INTEGER PRIMARY KEY,
  name         TEXT NOT NULL UNIQUE,
  key_hash     BLOB NOT NULL UNIQUE,
  created_at   TEXT NOT NULL,
  revoked_at   TEXT
);

CREATE TABLE pairings (                  -- admin service inserts (min id, max id)
  machine_a    INTEGER NOT NULL REFERENCES machines(id),
  machine_b    INTEGER NOT NULL REFERENCES machines(id),
  approved_at  TEXT NOT NULL,
  PRIMARY KEY (machine_a, machine_b),
  CHECK (machine_a < machine_b)
);

CREATE TABLE mailboxes (
  id           INTEGER PRIMARY KEY,
  machine_id   INTEGER NOT NULL REFERENCES machines(id),
  repo         TEXT NOT NULL,
  created_at   TEXT NOT NULL,
  UNIQUE (machine_id, repo)
);

CREATE TABLE sessions (
  id                INTEGER PRIMARY KEY,
  mailbox_id        INTEGER NOT NULL REFERENCES mailboxes(id),
  suffix            TEXT NOT NULL,
  agent             TEXT NOT NULL,       -- claude-code, codex, other
  agent_session_id  TEXT,                -- e.g. Claude Code session_id
  hooks             INTEGER NOT NULL,    -- has a human-turn hook
  cwd               TEXT NOT NULL,
  started_at        TEXT NOT NULL,
  last_seen         TEXT NOT NULL,
  ended_at          TEXT,
  max_depth_seen    INTEGER NOT NULL DEFAULT -1,
  last_delivered_at TEXT,
  UNIQUE (mailbox_id, suffix)
);
CREATE UNIQUE INDEX sessions_agent ON sessions (agent_session_id)
  WHERE agent_session_id IS NOT NULL;

CREATE TABLE messages (
  id               TEXT PRIMARY KEY,     -- time-ordered id
  to_mailbox_id    INTEGER NOT NULL REFERENCES mailboxes(id),
  to_session_id    INTEGER REFERENCES sessions(id),
  from_session_id  INTEGER NOT NULL REFERENCES sessions(id),
  body             TEXT NOT NULL,        -- max 8 KB
  depth            INTEGER NOT NULL,
  wake             INTEGER NOT NULL,
  reply_to         TEXT REFERENCES messages(id),
  sent_at          TEXT NOT NULL,
  expires_at       TEXT NOT NULL,
  delivered_to     INTEGER REFERENCES sessions(id),
  delivered_at     TEXT
);
-- Hot query: undelivered, unexpired mail for one mailbox, oldest first.
CREATE INDEX messages_queue ON messages (to_mailbox_id, sent_at)
  WHERE delivered_at IS NULL;

CREATE TABLE audit (                     -- append-only
  id           INTEGER PRIMARY KEY,
  at           TEXT NOT NULL,
  actor        TEXT NOT NULL,            -- machine:<name>, admin:web, admin:cli
  action       TEXT NOT NULL,
  detail       TEXT NOT NULL             -- JSON, never the message body
);
