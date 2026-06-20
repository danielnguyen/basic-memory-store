-- Additive Cluster 6 migration: proactive prefs, suggestions, feedback.

CREATE TABLE IF NOT EXISTS proactive_prefs (
  owner_id TEXT PRIMARY KEY,
  enabled BOOLEAN NOT NULL DEFAULT false,
  allowed_surfaces_json JSONB NOT NULL DEFAULT '[]'::jsonb,
  rule_prefs_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS proactive_suggestions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id TEXT NOT NULL,
  source_event_log_id UUID REFERENCES event_ingest_log(id) ON DELETE SET NULL,
  source_type TEXT NOT NULL,
  kind TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'dismissed', 'accepted', 'expired')),
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  explanation_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  evidence_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  target_surface TEXT,
  delivery_surface TEXT,
  delivery_status TEXT NOT NULL DEFAULT 'not_attempted' CHECK (delivery_status IN ('not_attempted', 'delivered', 'failed')),
  delivery_external_id TEXT,
  delivery_error TEXT,
  delivered_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (owner_id, source_event_log_id, kind)
);

CREATE INDEX IF NOT EXISTS idx_proactive_suggestions_owner_status_time
  ON proactive_suggestions(owner_id, status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_proactive_suggestions_owner_surface_status_time
  ON proactive_suggestions(owner_id, target_surface, status, created_at DESC);

CREATE TABLE IF NOT EXISTS proactive_feedback (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  suggestion_id UUID NOT NULL REFERENCES proactive_suggestions(id) ON DELETE CASCADE,
  owner_id TEXT NOT NULL,
  feedback_type TEXT NOT NULL CHECK (feedback_type IN ('dismissed', 'useful', 'not_useful', 'accepted')),
  reason TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_proactive_feedback_suggestion_time
  ON proactive_feedback(suggestion_id, created_at DESC);
