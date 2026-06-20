-- Additive migration for file ingestion.

ALTER TABLE artifacts
  ADD COLUMN IF NOT EXISTS ingestion_id UUID,
  ADD COLUMN IF NOT EXISTS source_kind TEXT,
  ADD COLUMN IF NOT EXISTS repo_name TEXT,
  ADD COLUMN IF NOT EXISTS repo_ref TEXT,
  ADD COLUMN IF NOT EXISTS file_path TEXT;

CREATE INDEX IF NOT EXISTS idx_artifacts_ingestion
  ON artifacts(ingestion_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_artifacts_owner_file_path
  ON artifacts(owner_id, file_path);
