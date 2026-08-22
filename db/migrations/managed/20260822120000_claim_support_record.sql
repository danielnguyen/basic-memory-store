ALTER TABLE claim_records
  ADD COLUMN IF NOT EXISTS presented_to_user BOOLEAN NOT NULL DEFAULT TRUE,
  ADD COLUMN IF NOT EXISTS support_json JSONB;

ALTER TABLE claim_records
  DROP CONSTRAINT IF EXISTS claim_records_schema_version_check;

ALTER TABLE claim_records
  ADD CONSTRAINT claim_records_schema_version_check CHECK (
    schema_version IN ('claim-record.v1', 'claim-record.v2')
  );

ALTER TABLE claim_records
  ADD CONSTRAINT claim_records_support_version_check CHECK (
    (
      schema_version = 'claim-record.v1'
      AND presented_to_user = TRUE
      AND support_json IS NULL
    )
    OR (
      schema_version = 'claim-record.v2'
      AND presented_to_user = FALSE
      AND jsonb_typeof(support_json) = 'object'
      AND octet_length(support_json::text) <= 32768
      AND support_json ?& ARRAY[
        'claim_digest', 'supporting_evidence_ref_ids', 'counterevidence_ref_ids',
        'material_exclusions', 'executed_derivations', 'material_scope_limitations',
        'calibration_status', 'conclusion_disposition', 'qualification_required',
        'limitation_codes'
      ]
      AND support_json - ARRAY[
        'claim_digest', 'supporting_evidence_ref_ids', 'counterevidence_ref_ids',
        'material_exclusions', 'executed_derivations', 'material_scope_limitations',
        'calibration_status', 'conclusion_disposition', 'qualification_required',
        'limitation_codes'
      ] = '{}'::jsonb
    )
  );
