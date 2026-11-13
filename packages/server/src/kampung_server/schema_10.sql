-- Kampung schema, version 10: session labels. The git branch the client
-- last reported for the session's folder, and the client version that last
-- registered it. Both are self-reported and shown as information only.
-- Old rows, and sessions from clients before 0.6, have both NULL.
ALTER TABLE sessions ADD COLUMN branch TEXT;
ALTER TABLE sessions ADD COLUMN client_version TEXT;
