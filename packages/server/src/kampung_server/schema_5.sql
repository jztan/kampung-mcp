-- Kampung schema, version 5: the loop guard's wake budgets replace depth.
-- One row per pair of mailboxes that is draining: full_at is when its bucket
-- is full again. No row means full. Old messages keep their depth; new ones
-- store -1 and say why they did not wake.
CREATE TABLE pair_budgets (
  mailbox_a  INTEGER NOT NULL REFERENCES mailboxes(id) ON DELETE CASCADE,
  mailbox_b  INTEGER NOT NULL REFERENCES mailboxes(id) ON DELETE CASCADE,
  full_at    TEXT NOT NULL,
  PRIMARY KEY (mailbox_a, mailbox_b),
  CHECK (mailbox_a <= mailbox_b)
);
CREATE INDEX pair_budgets_b ON pair_budgets (mailbox_b);
CREATE INDEX pair_budgets_full ON pair_budgets (full_at);
ALTER TABLE mailboxes ADD COLUMN wakes_since_refill INTEGER NOT NULL DEFAULT 0;
ALTER TABLE mailboxes ADD COLUMN connector_refill_at TEXT;
-- When the session last reported a typed prompt; NULL if it never has
-- (no hooks, or hooks that never run, such as Codex on Windows).
ALTER TABLE sessions ADD COLUMN typed_at TEXT;
ALTER TABLE messages ADD COLUMN wake_reason TEXT;
ALTER TABLE messages ADD COLUMN spent INTEGER NOT NULL DEFAULT 0;
ALTER TABLE messages ADD COLUMN fyi INTEGER NOT NULL DEFAULT 0;
ALTER TABLE messages ADD COLUMN budget_left INTEGER;
-- The client address a pending sign-in came from, so one address keeps only
-- its newest few and cannot fill the cap for everyone.
ALTER TABLE oauth_requests ADD COLUMN client TEXT NOT NULL DEFAULT '';
CREATE INDEX oauth_requests_client ON oauth_requests (client);
-- An agent session id is unique per machine, not across them, so a key that
-- learns another machine's id cannot register it first. machine_id copies the
-- mailbox's machine (mailboxes never move); sessions._create sets it.
ALTER TABLE sessions ADD COLUMN machine_id INTEGER REFERENCES machines(id);
UPDATE sessions SET machine_id =
  (SELECT mb.machine_id FROM mailboxes mb WHERE mb.id = sessions.mailbox_id);
DROP INDEX sessions_agent;
CREATE UNIQUE INDEX sessions_agent ON sessions (machine_id, agent_session_id)
  WHERE agent_session_id IS NOT NULL;
