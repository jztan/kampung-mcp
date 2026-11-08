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
ALTER TABLE mailboxes ADD COLUMN wakes_since_refill INTEGER NOT NULL DEFAULT 0;
ALTER TABLE mailboxes ADD COLUMN connector_refill_at TEXT;
ALTER TABLE messages ADD COLUMN wake_reason TEXT;
ALTER TABLE messages ADD COLUMN spent INTEGER NOT NULL DEFAULT 0;
ALTER TABLE messages ADD COLUMN fyi INTEGER NOT NULL DEFAULT 0;
ALTER TABLE messages ADD COLUMN budget_left INTEGER;
