-- Kampung schema, version 6: send modes. A message sent with mode "tell"
-- wakes its receiver like "ask" but needs no reply; fyi still means neither.
-- Old rows keep 0, and an fyi row is read as fyi whatever this says.
ALTER TABLE messages ADD COLUMN no_reply INTEGER NOT NULL DEFAULT 0;
