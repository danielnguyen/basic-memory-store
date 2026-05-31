-- Additive Cluster 9A migration: R20 manual memory promotion, reinforcement, and audit trail.

CREATE TABLE IF NOT EXISTS memory_items (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id TEXT NOT NULL,
  memory_type TEXT NOT NULL,
  summary TEXT NOT NULL,
  source_refs_json JSONB NOT NULL DEFAULT '[]'::jsonb,
  source_ref_hash TEXT NOT NULL,
  scores_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  promotion_state TEXT NOT NULL DEFAULT 'promoted' CHECK (promotion_state IN ('candidate', 'promoted', 'suppressed', 'decayed')),
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'stale', 'invalidated', 'superseded', 'expired')),
  supersedes_memory_id UUID REFERENCES memory_items(id) ON DELETE SET NULL,
  superseded_by_memory_id UUID REFERENCES memory_items(id) ON DELETE SET NULL,
  last_reinforced_at TIMESTAMPTZ,
  expires_at TIMESTAMPTZ,
  derivation_version TEXT NOT NULL DEFAULT 'r20-mvp-v1',
  confidence DOUBLE PRECISION CHECK (confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)),
  explanation_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  generation_trace_id TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_items_owner_source_hash_active
  ON memory_items(owner_id, source_ref_hash)
  WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_memory_items_owner_status_time
  ON memory_items(owner_id, status, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_memory_items_supersedes
  ON memory_items(supersedes_memory_id);

CREATE INDEX IF NOT EXISTS idx_memory_items_superseded_by
  ON memory_items(superseded_by_memory_id);

CREATE TABLE IF NOT EXISTS memory_events (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  memory_id UUID NOT NULL REFERENCES memory_items(id) ON DELETE CASCADE,
  owner_id TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK (event_type IN (
    'created',
    'updated',
    'reinforced',
    'superseded',
    'expired',
    'promoted',
    'suppressed',
    'decayed',
    'state_changed'
  )),
  reason_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_memory_events_memory_time
  ON memory_events(memory_id, created_at ASC);

CREATE INDEX IF NOT EXISTS idx_memory_events_owner_type_time
  ON memory_events(owner_id, event_type, created_at DESC);
