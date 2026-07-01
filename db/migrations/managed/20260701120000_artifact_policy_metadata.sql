ALTER TABLE artifacts
  ADD COLUMN IF NOT EXISTS policy_metadata JSONB;
