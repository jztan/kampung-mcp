-- Kampung schema, version 3: the Claude app connector. One fixed OAuth
-- client lives in code, so there is no client table.

-- One-time codes printed by `kampung connect`, stored hashed.
CREATE TABLE oauth_connect_codes (
  id          INTEGER PRIMARY KEY,
  machine_id  INTEGER NOT NULL REFERENCES machines(id),
  code_hash   BLOB NOT NULL UNIQUE,
  expires_at  TEXT NOT NULL,
  used_at     TEXT
);
CREATE INDEX oauth_connect_codes_expiry ON oauth_connect_codes (expires_at);

-- Authorize requests waiting for a code on the connect page.
CREATE TABLE oauth_requests (
  request_id  TEXT PRIMARY KEY,
  params_json TEXT NOT NULL,
  attempts    INTEGER NOT NULL DEFAULT 0,
  expires_at  TEXT NOT NULL
);
CREATE INDEX oauth_requests_expiry ON oauth_requests (expires_at);

-- One grant per machine; deleting it deletes its tokens. AUTOINCREMENT so a
-- new grant never takes a deleted one's id.
CREATE TABLE oauth_grants (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  machine_id  INTEGER NOT NULL UNIQUE REFERENCES machines(id),
  created_at  TEXT NOT NULL
);

CREATE TABLE oauth_tokens (
  grant_id    INTEGER NOT NULL REFERENCES oauth_grants(id) ON DELETE CASCADE,
  kind        TEXT NOT NULL,            -- code, access, refresh
  token_hash  BLOB NOT NULL UNIQUE,
  record_json TEXT NOT NULL,
  expires_at  TEXT NOT NULL,
  used_at     TEXT
);
CREATE INDEX oauth_tokens_grant ON oauth_tokens (grant_id);
CREATE INDEX oauth_tokens_expiry ON oauth_tokens (expires_at);
