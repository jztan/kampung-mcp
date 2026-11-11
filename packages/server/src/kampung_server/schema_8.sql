-- Kampung schema, version 8: idle wake. A Claude Code session whose client
-- can wake it while idle (a watched wake file and a FileChanged hook) says so
-- when it registers. A background session with this flag can be woken, so a
-- send to it no longer warns of a late reply. Old rows have it off.
ALTER TABLE sessions ADD COLUMN idle_wake INTEGER NOT NULL DEFAULT 0;
