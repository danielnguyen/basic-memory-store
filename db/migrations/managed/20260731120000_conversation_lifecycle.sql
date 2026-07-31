ALTER TABLE conversations
  ADD COLUMN IF NOT EXISTS lifecycle_state TEXT;

ALTER TABLE conversations
  ADD COLUMN IF NOT EXISTS superseded_by_conversation_id UUID;

UPDATE conversations
SET lifecycle_state = 'open'
WHERE lifecycle_state IS NULL;

ALTER TABLE conversations
  ALTER COLUMN lifecycle_state SET DEFAULT 'open',
  ALTER COLUMN lifecycle_state SET NOT NULL;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'conversations_superseded_by_conversation_id_fkey'
      AND conrelid = 'conversations'::regclass
  ) THEN
    ALTER TABLE conversations
      ADD CONSTRAINT conversations_superseded_by_conversation_id_fkey
      FOREIGN KEY (superseded_by_conversation_id)
      REFERENCES conversations(id)
      ON DELETE RESTRICT;
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'conversations_lifecycle_state_check'
      AND conrelid = 'conversations'::regclass
  ) THEN
    ALTER TABLE conversations
      ADD CONSTRAINT conversations_lifecycle_state_check
      CHECK (lifecycle_state IN ('open', 'closed', 'superseded'));
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'conversations_lifecycle_replacement_check'
      AND conrelid = 'conversations'::regclass
  ) THEN
    ALTER TABLE conversations
      ADD CONSTRAINT conversations_lifecycle_replacement_check CHECK (
        (lifecycle_state IN ('open', 'closed') AND superseded_by_conversation_id IS NULL)
        OR (lifecycle_state = 'superseded' AND superseded_by_conversation_id IS NOT NULL)
      );
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'conversations_replacement_not_self_check'
      AND conrelid = 'conversations'::regclass
  ) THEN
    ALTER TABLE conversations
      ADD CONSTRAINT conversations_replacement_not_self_check
      CHECK (superseded_by_conversation_id IS NULL OR superseded_by_conversation_id <> id);
  END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_conversations_owner_lifecycle_activity
  ON conversations(owner_id, lifecycle_state, updated_at DESC, id DESC);
