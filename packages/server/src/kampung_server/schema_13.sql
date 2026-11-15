-- Kampung schema, version 13: a session's latest step ("now"), which its hooks
-- report from what the session does (a command's description, the file it is
-- editing), and when it was reported. Shown to peers as information only, in
-- place of the summary its agent had to set by hand (set_summary, removed).
-- Cleared when an ended session is revived. Old rows have NULL; the summary
-- column stays, unused, since a pushed schema is never edited.
ALTER TABLE sessions ADD COLUMN activity TEXT;
ALTER TABLE sessions ADD COLUMN activity_at TEXT;
