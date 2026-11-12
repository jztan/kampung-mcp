-- Kampung schema, version 9: owner instructions. A Claude app grant the
-- operator marks with `kampung owner add` may send mode "instruct". The flag
-- sits on the grant, so it goes when the grant goes and a new grant starts
-- without it. A message sent that way is stamped owner = 1. Old rows have
-- both off.
ALTER TABLE oauth_grants ADD COLUMN owner INTEGER NOT NULL DEFAULT 0;
ALTER TABLE messages ADD COLUMN owner INTEGER NOT NULL DEFAULT 0;
