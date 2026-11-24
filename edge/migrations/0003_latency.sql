ALTER TABLE events ADD COLUMN created_ms INTEGER NOT NULL DEFAULT 0;
ALTER TABLE events ADD COLUMN started_ms INTEGER NOT NULL DEFAULT 0;
ALTER TABLE events ADD COLUMN finished_ms INTEGER NOT NULL DEFAULT 0;
CREATE INDEX events_recent ON events(created_ms DESC);
CREATE UNIQUE INDEX one_active_delivery_per_owner ON events(owner)
WHERE type='delivery' AND state IN ('pending','processing','uncertain');
