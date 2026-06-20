-- Ensure pinned memories can be global and survive conversation deletion
DO $$
DECLARE
  fk_name TEXT;
BEGIN
  IF to_regclass('pinned_memories') IS NULL THEN
    RETURN;
  END IF;

  ALTER TABLE pinned_memories
    ALTER COLUMN conversation_id DROP NOT NULL;

  FOR fk_name IN
    SELECT c.conname
    FROM pg_constraint c
    JOIN pg_class t ON t.oid = c.conrelid
    JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(c.conkey)
    WHERE t.relname = 'pinned_memories'
      AND c.contype = 'f'
      AND a.attname = 'conversation_id'
  LOOP
    EXECUTE format('ALTER TABLE pinned_memories DROP CONSTRAINT IF EXISTS %I', fk_name);
  END LOOP;

  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'pinned_memories_conversation_id_set_null_fkey'
  ) THEN
    ALTER TABLE pinned_memories
      ADD CONSTRAINT pinned_memories_conversation_id_set_null_fkey
      FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE SET NULL;
  END IF;
END $$;
