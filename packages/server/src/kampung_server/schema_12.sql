-- Kampung schema, version 12: a session's summary, a few words its own agent
-- sets on what it is doing (set_summary), shown to peers as information only.
-- Cleared when an ended session is revived. Old rows have NULL.
ALTER TABLE sessions ADD COLUMN summary TEXT;
