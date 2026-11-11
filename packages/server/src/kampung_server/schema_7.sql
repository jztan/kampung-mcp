-- Kampung schema, version 7: background sessions. Claude Code runs some
-- conversations in the background (claude --bg, /background, agents). Such a
-- session cannot be woken by Channels and leaves mailbox mail to a live
-- foreground session in the same mailbox. Old rows are foreground.
ALTER TABLE sessions ADD COLUMN background INTEGER NOT NULL DEFAULT 0;
