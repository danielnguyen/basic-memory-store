-- Additive Cluster 9C migration: R22 recall selection and mentionability decisions.

CREATE TABLE IF NOT EXISTS recall_decisions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  request_id TEXT NOT NULL,
  owner_id TEXT NOT NULL,
  candidate_id TEXT NOT NULL,
  candidate_type TEXT NOT NULL CHECK (candidate_type IN (
    'memory_item',
    'episode',
    'message',
    'artifact',
    'event',
    'derived_text'
  )),
  candidate_ref_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  source_refs_json JSONB NOT NULL DEFAULT '[]'::jsonb,
  scene_id TEXT,
  surface TEXT,
  urgency TEXT,
  sensitivity TEXT,
  relevance_score DOUBLE PRECISION CHECK (relevance_score IS NULL OR (relevance_score >= 0.0 AND relevance_score <= 1.0)),
  salience_score DOUBLE PRECISION CHECK (salience_score IS NULL OR (salience_score >= 0.0 AND salience_score <= 1.0)),
  recency_score DOUBLE PRECISION CHECK (recency_score IS NULL OR (recency_score >= 0.0 AND recency_score <= 1.0)),
  mentionability_score DOUBLE PRECISION NOT NULL CHECK (mentionability_score >= 0.0 AND mentionability_score <= 1.0),
  decision TEXT NOT NULL CHECK (decision IN ('mention', 'suppress', 'implicit_only')),
  mention_strategy TEXT NOT NULL CHECK (mention_strategy IN ('none', 'implicit', 'light_callback', 'explicit_callback')),
  prompt_eligible BOOLEAN NOT NULL DEFAULT false,
  reason_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_recall_decisions_request_candidate
  ON recall_decisions(request_id, owner_id, candidate_type, candidate_id);

CREATE INDEX IF NOT EXISTS idx_recall_decisions_request_debug
  ON recall_decisions(request_id, owner_id, created_at ASC, id ASC);

CREATE INDEX IF NOT EXISTS idx_recall_decisions_owner_candidate_time
  ON recall_decisions(owner_id, candidate_type, created_at DESC);
