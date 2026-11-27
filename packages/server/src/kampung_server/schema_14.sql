-- Kampung schema, version 14: session names. A session's hooks report the
-- title its agent shows the person (Claude Code's session title, Codex's
-- thread name, or KAMPUNG_NAME), stored made into a name in `title`, with
-- `title_kind` saying where it came from ('auto', 'rename' or 'env'). The
-- owner's Claude app may set `owner_name`, which wins; `owner_title_seen` is
-- the reported /rename title at that moment, so a newer /rename replaces it.
-- `name` is the one in use: unique within a mailbox among open sessions, so
-- machine/repo@name always means one session. Old rows have NULL.
ALTER TABLE sessions ADD COLUMN title TEXT;
ALTER TABLE sessions ADD COLUMN title_kind TEXT;
ALTER TABLE sessions ADD COLUMN owner_name TEXT;
ALTER TABLE sessions ADD COLUMN owner_title_seen TEXT;
ALTER TABLE sessions ADD COLUMN name TEXT;
CREATE UNIQUE INDEX sessions_open_name ON sessions (mailbox_id, name)
    WHERE ended_at IS NULL AND name IS NOT NULL;
-- A session's former names, for the hint when mail uses an old one. They
-- never route mail. The last few per session are kept, for a week.
CREATE TABLE session_names (
    session_id INTEGER NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    retired_at TEXT NOT NULL
);
CREATE INDEX session_names_by_session ON session_names (session_id, retired_at);
-- The sweep drops former names by age, inside the write lock: no full scan.
CREATE INDEX session_names_by_age ON session_names (retired_at);
