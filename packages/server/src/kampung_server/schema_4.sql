-- Kampung schema, version 4: one optional attachment per message, kept apart
-- from the body so inbox and peek stay fast. It lives and dies with its
-- message.
CREATE TABLE attachments (
  message_id  TEXT PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
  name        TEXT NOT NULL,
  type        TEXT NOT NULL CHECK (type IN ('text', 'image/png', 'image/jpeg')),
  bytes       INTEGER NOT NULL,
  data        BLOB NOT NULL
);
