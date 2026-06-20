-- Additive migration for R02/R03/R04
-- Safe to apply on top of existing schema.sql

CREATE TABLE IF NOT EXISTS artifacts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id TEXT NOT NULL,
  client_id TEXT,
  conversation_id UUID REFERENCES conversations(id) ON DELETE SET NULL,
  sha256 TEXT,
  mime TEXT NOT NULL,
  size BIGINT NOT NULL CHECK (size >= 0),
  object_uri TEXT NOT NULL,
  source_surface TEXT,
  filename TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'completed', 'failed')),
  content_hash_version TEXT NOT NULL DEFAULT 'v1',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_artifacts_owner_time
  ON artifacts(owner_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_artifacts_convo_time
  ON artifacts(conversation_id, created_at DESC);

CREATE TABLE IF NOT EXISTS artifact_links (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  artifact_id UUID NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
  conversation_id UUID REFERENCES conversations(id) ON DELETE CASCADE,
  message_id UUID REFERENCES messages(id) ON DELETE CASCADE,
  relationship TEXT NOT NULL DEFAULT 'referenced',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_artifact_links_artifact
  ON artifact_links(artifact_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_artifact_links_conversation
  ON artifact_links(conversation_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_artifact_links_message
  ON artifact_links(message_id, created_at DESC);

CREATE TABLE IF NOT EXISTS derived_text (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  artifact_id UUID NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  language TEXT,
  text TEXT NOT NULL,
  derivation_params JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_derived_text_artifact_time
  ON derived_text(artifact_id, created_at DESC);

CREATE TABLE IF NOT EXISTS embeddings (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  ref_type TEXT NOT NULL CHECK (ref_type IN ('message', 'derived_text')),
  ref_id UUID NOT NULL,
  model TEXT NOT NULL,
  qdrant_point_id TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_embeddings_ref
  ON embeddings(ref_type, ref_id, created_at DESC);

CREATE TABLE IF NOT EXISTS traces (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  trace_id TEXT NOT NULL,
  request_id TEXT NOT NULL UNIQUE,
  conversation_id UUID REFERENCES conversations(id) ON DELETE SET NULL,
  owner_id TEXT,
  surface TEXT,
  router_decision_json JSONB,
  retrieval_json JSONB,
  model_calls_json JSONB,
  cost_json JSONB,
  latency_ms INTEGER,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_traces_conversation_time
  ON traces(conversation_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_traces_owner_time
  ON traces(owner_id, created_at DESC);

CREATE TABLE IF NOT EXISTS pinned_memories (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id TEXT NOT NULL,
  conversation_id UUID NULL REFERENCES conversations(id) ON DELETE SET NULL,
  content TEXT NOT NULL,
  metadata JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pinned_memories_owner_time
  ON pinned_memories(owner_id, created_at DESC);

CREATE TABLE IF NOT EXISTS policy_overlays (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id TEXT NOT NULL,
  surface TEXT,
  policy_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_policy_overlays_owner_surface
  ON policy_overlays(owner_id, surface, created_at DESC);

CREATE TABLE IF NOT EXISTS persona_overlays (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id TEXT NOT NULL,
  surface TEXT,
  persona_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_persona_overlays_owner_surface
  ON persona_overlays(owner_id, surface, created_at DESC);
