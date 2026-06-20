-- Additive Cluster 4 migration: time-aware retrieval support, hygiene flags, graph core.

CREATE TABLE IF NOT EXISTS memory_hygiene_flags (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id TEXT NOT NULL,
  subject_type TEXT NOT NULL,
  subject_id UUID,
  flag_type TEXT NOT NULL,
  details_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  resolved_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_memory_hygiene_flags_owner_status
  ON memory_hygiene_flags(owner_id, status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_memory_hygiene_flags_owner_type
  ON memory_hygiene_flags(owner_id, flag_type, created_at DESC);

CREATE TABLE IF NOT EXISTS memory_entities (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id TEXT NOT NULL,
  entity_type TEXT NOT NULL,
  canonical_name TEXT NOT NULL,
  normalized_key TEXT NOT NULL,
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (owner_id, entity_type, normalized_key)
);

CREATE INDEX IF NOT EXISTS idx_memory_entities_owner_type
  ON memory_entities(owner_id, entity_type, canonical_name);

CREATE TABLE IF NOT EXISTS memory_edges (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id TEXT NOT NULL,
  from_entity_id UUID NOT NULL REFERENCES memory_entities(id) ON DELETE CASCADE,
  to_entity_id UUID NOT NULL REFERENCES memory_entities(id) ON DELETE CASCADE,
  edge_type TEXT NOT NULL,
  observed_at TIMESTAMPTZ,
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_memory_edges_owner_type
  ON memory_edges(owner_id, edge_type, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_memory_edges_from
  ON memory_edges(from_entity_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_memory_edges_to
  ON memory_edges(to_entity_id, created_at DESC);
