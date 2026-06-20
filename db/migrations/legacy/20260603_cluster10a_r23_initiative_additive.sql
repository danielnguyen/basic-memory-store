-- Additive Cluster 10A / R23 migration: initiative decision and explainability records.

CREATE TABLE IF NOT EXISTS initiative_events (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  source_event_log_id UUID REFERENCES event_ingest_log(id) ON DELETE SET NULL,
  trigger_type TEXT NOT NULL,
  trigger_ref_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (owner_id, request_id)
);

CREATE INDEX IF NOT EXISTS idx_initiative_events_owner_time
  ON initiative_events(owner_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_initiative_events_source_event
  ON initiative_events(source_event_log_id, created_at DESC);

CREATE TABLE IF NOT EXISTS initiative_decisions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  initiative_event_id UUID NOT NULL REFERENCES initiative_events(id) ON DELETE CASCADE,
  owner_id TEXT NOT NULL,
  proactive_suggestion_id UUID REFERENCES proactive_suggestions(id) ON DELETE SET NULL,
  decision_status TEXT NOT NULL CHECK (decision_status IN ('created', 'suppressed', 'no_op')),
  score DOUBLE PRECISION,
  reason_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  delivery_surface TEXT,
  delivery_status TEXT NOT NULL DEFAULT 'not_attempted' CHECK (delivery_status IN ('not_attempted', 'delivered', 'failed')),
  suppression_reason TEXT,
  cooldown_identity_key TEXT,
  normalized_subject TEXT,
  cooldown_until TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_initiative_decisions_event_time
  ON initiative_decisions(initiative_event_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_initiative_decisions_owner_time
  ON initiative_decisions(owner_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_initiative_decisions_cooldown_key_time
  ON initiative_decisions(owner_id, cooldown_identity_key, created_at DESC);

CREATE TABLE IF NOT EXISTS initiative_feedback (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  decision_id UUID NOT NULL REFERENCES initiative_decisions(id) ON DELETE CASCADE,
  proactive_feedback_id UUID REFERENCES proactive_feedback(id) ON DELETE SET NULL,
  owner_id TEXT NOT NULL,
  feedback_type TEXT NOT NULL CHECK (feedback_type IN ('dismissed', 'useful', 'not_useful', 'accepted')),
  feedback_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_initiative_feedback_decision_time
  ON initiative_feedback(decision_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_initiative_feedback_owner_time
  ON initiative_feedback(owner_id, created_at DESC);
