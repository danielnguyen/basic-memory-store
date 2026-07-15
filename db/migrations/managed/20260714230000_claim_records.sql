CREATE TABLE IF NOT EXISTS claim_records (
  claim_id TEXT PRIMARY KEY,
  schema_version TEXT NOT NULL CHECK (schema_version = 'claim-record.v1'),
  owner_id TEXT NOT NULL CHECK (
    char_length(owner_id) BETWEEN 1 AND 120
    AND owner_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]*$'
  ),
  conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  request_id TEXT NOT NULL CHECK (
    char_length(request_id) BETWEEN 1 AND 120
    AND request_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]*$'
  ),
  assistant_message_id UUID NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  surface TEXT NOT NULL CHECK (char_length(surface) BETWEEN 1 AND 64),
  runtime_session_id TEXT NOT NULL CHECK (
    char_length(runtime_session_id) BETWEEN 1 AND 120
    AND runtime_session_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]*$'
  ),
  runtime_turn_id TEXT NOT NULL CHECK (
    char_length(runtime_turn_id) BETWEEN 1 AND 120
    AND runtime_turn_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]*$'
  ),
  claim_anchor TEXT NOT NULL CHECK (char_length(claim_anchor) BETWEEN 1 AND 500),
  claim_anchor_digest TEXT NOT NULL
    CHECK (claim_anchor_digest ~ '^sha256:[0-9a-f]{64}$'),
  claim_class TEXT NOT NULL CHECK (
    claim_class IN (
      'verified_fact',
      'source_backed_fact',
      'manufacturer_guidance',
      'expert_consensus',
      'runtime_inference',
      'speculation',
      'unknown'
    )
  ),
  calibration_status TEXT NOT NULL
    CHECK (calibration_status IN ('supported', 'limited', 'unsupported')),
  evidence_strength TEXT NOT NULL
    CHECK (evidence_strength IN ('strong', 'moderate', 'weak', 'none')),
  confidence TEXT NOT NULL CHECK (confidence IN ('high', 'medium', 'low', 'unknown')),
  strongest_authority TEXT NOT NULL CHECK (
    strongest_authority IN (
      'peer_reviewed_evidence',
      'clinical_guidance',
      'manufacturer_guidance',
      'tool_output',
      'trusted_integration',
      'user_report',
      'runtime_inference',
      'speculation',
      'unknown'
    )
  ),
  freshness_summary TEXT NOT NULL
    CHECK (freshness_summary IN ('current', 'mixed', 'stale', 'unknown', 'not_applicable')),
  uncertainty_disclosure_required BOOLEAN NOT NULL,
  evidence_references_json JSONB NOT NULL CHECK (
    jsonb_typeof(evidence_references_json) = 'array'
    AND jsonb_array_length(evidence_references_json) <= 16
  ),
  limitation_codes_json JSONB NOT NULL CHECK (
    jsonb_typeof(limitation_codes_json) = 'array'
    AND jsonb_array_length(limitation_codes_json) <= 10
  ),
  user_safe_summary TEXT NOT NULL CHECK (char_length(user_safe_summary) BETWEEN 1 AND 500),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (
    char_length(claim_id) BETWEEN 1 AND 120
    AND claim_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]*$'
  )
);

CREATE INDEX IF NOT EXISTS idx_claim_records_owner_conversation_newest
  ON claim_records(owner_id, conversation_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_claim_records_assistant_message
  ON claim_records(assistant_message_id, created_at ASC, claim_id ASC);

CREATE INDEX IF NOT EXISTS idx_claim_records_owner_request
  ON claim_records(owner_id, request_id, created_at ASC);
