-- Drop legacy Redmine / ticket schema (run once per environment).
-- Safe to re-run: procedures ignore missing objects.

ALTER TABLE lesite DROP COLUMN IF EXISTS redmine_url;
DROP TABLE IF EXISTS ticket_overview;
DROP TABLE IF EXISTS ticket_siteview;
