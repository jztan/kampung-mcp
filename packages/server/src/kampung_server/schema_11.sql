-- Kampung schema, version 11: a lost owner mark. Set when a Claude app grant
-- that had the owner mark is replaced or deleted, so the owner hears of it;
-- cleared by kampung owner add or owner remove. Old rows have both NULL.
ALTER TABLE machines ADD COLUMN owner_lost_at TEXT;
ALTER TABLE machines ADD COLUMN owner_lost_reason TEXT;
