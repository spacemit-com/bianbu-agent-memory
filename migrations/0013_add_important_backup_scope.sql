-- Extend the shipped backup scope list with `important`, the tag the user
-- designates a MEM-L3 record with (MEM-L3 `important` mark). It is a scope of
-- its own because the mark is orthogonal to the record's content category.
--
-- Only a row still holding an untouched shipped default is rewritten, the same
-- "touch exactly what the release wrote" rule `converged_embedding` and
-- `converged_backup_scopes` already follow. A list chosen deliberately is left
-- alone: adding an upload category to an account that already enabled backup
-- would silently enlarge what that account uploads.
UPDATE memory_settings
SET backup_scopes_json =
    '["preferences","development_habits","schedule_habits","important"]'
WHERE backup_scopes_json IN (
    '["preferences","development_habits","schedule_habits"]',
    '["summaries","preferences","development_habits","schedule_habits"]'
);
