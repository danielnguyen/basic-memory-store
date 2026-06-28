ALTER TABLE memory_items
  ALTER COLUMN derivation_version SET DEFAULT 'memory-promotion-v1';

ALTER TABLE episodes
  ALTER COLUMN derivation_version SET DEFAULT 'episode-construction-v1';

UPDATE memory_items
SET derivation_version = 'memory-promotion-v1'
WHERE derivation_version = 'r20-mvp-v1';

UPDATE episodes
SET derivation_version = 'episode-construction-v1'
WHERE derivation_version = 'r21-m0-v1';

