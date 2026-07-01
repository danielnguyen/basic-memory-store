ALTER TABLE messages
  ADD COLUMN IF NOT EXISTS policy_metadata JSONB;

ALTER TABLE artifacts
  ADD COLUMN IF NOT EXISTS policy_metadata JSONB;

ALTER TABLE pinned_memories
  ADD COLUMN IF NOT EXISTS policy_metadata JSONB;

ALTER TABLE policy_overlays
  ADD COLUMN IF NOT EXISTS policy_metadata JSONB;

ALTER TABLE persona_overlays
  ADD COLUMN IF NOT EXISTS policy_metadata JSONB;
