ALTER TABLE claim_records
  ADD COLUMN IF NOT EXISTS acquisition_manifest_id TEXT;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'claim_records_acquisition_manifest_id_check'
      AND conrelid = 'claim_records'::regclass
  ) THEN
    ALTER TABLE claim_records
      ADD CONSTRAINT claim_records_acquisition_manifest_id_check CHECK (
        acquisition_manifest_id IS NULL
        OR (
          char_length(acquisition_manifest_id) BETWEEN 1 AND 120
          AND acquisition_manifest_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]*$'
        )
      );
  END IF;
END
$$;
