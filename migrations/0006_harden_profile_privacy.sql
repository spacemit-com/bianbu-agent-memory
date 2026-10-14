-- Profile records contain PII and must never be eligible for the derived
-- semantic projection, including records created by older releases.
UPDATE long_term_memories
SET sensitivity = 'sensitive'
WHERE kind = 'profile'
  AND sensitivity IN ('public', 'private');

UPDATE semantic_projection_mappings
SET state = 'delete_proposed',
    updated_at = CAST(strftime('%s', 'now') AS INTEGER) * 1000
WHERE state = 'active'
  AND memory_id IN (
      SELECT memory_id
      FROM long_term_memories
      WHERE kind = 'profile'
         OR sensitivity NOT IN ('public', 'private')
         OR tags_json LIKE '%"slot:profile.%"%'
  );
