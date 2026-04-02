-- Basic Memory Store
-- Authoritative Postgres schema
-- All conversational state derives from this schema
-- Vector indices are disposable and rebuildable

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS conversations (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id TEXT NOT NULL,
  client_id TEXT,
  title TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS messages (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  owner_id TEXT NOT NULL,
  client_id TEXT,
  role TEXT NOT NULL CHECK (role IN ('user','assistant','system','tool')),
  content TEXT NOT NULL,
  metadata JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_messages_owner_time
  ON messages(owner_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_messages_convo_time
  ON messages(conversation_id, created_at DESC);

-- Artifact metadata (blob storage remains external/object-store)
CREATE TABLE IF NOT EXISTS artifacts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id TEXT NOT NULL,
  client_id TEXT,
  conversation_id UUID REFERENCES conversations(id) ON DELETE SET NULL,
  ingestion_id UUID,
  sha256 TEXT,
  mime TEXT NOT NULL,
  size BIGINT NOT NULL CHECK (size >= 0),
  object_uri TEXT NOT NULL,
  source_surface TEXT,
  source_kind TEXT,
  filename TEXT NOT NULL,
  repo_name TEXT,
  repo_ref TEXT,
  file_path TEXT,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'completed', 'failed')),
  content_hash_version TEXT NOT NULL DEFAULT 'v1',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_artifacts_owner_time
  ON artifacts(owner_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_artifacts_convo_time
  ON artifacts(conversation_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_artifacts_ingestion
  ON artifacts(ingestion_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_artifacts_owner_file_path
  ON artifacts(owner_id, file_path);

-- Explicit linkage between artifacts and message/conversation entities
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

-- Rebuildable textual derivations of artifacts (captions, OCR text, summaries)
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

-- Embedding pointer metadata. Vector payload remains in Qdrant.
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

-- End-to-end request traces for retrieval/routing/model-call observability
CREATE TABLE IF NOT EXISTS traces (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  request_id TEXT NOT NULL UNIQUE,
  conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  owner_id TEXT NOT NULL,
  client_id TEXT,
  surface TEXT NOT NULL,
  profile_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  retrieval_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  router_decision_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  manual_override_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  model_call_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  fallback_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  cost_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  latency_ms INTEGER,
  status TEXT NOT NULL CHECK (status IN ('ok', 'degraded', 'failed')),
  error_text TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_traces_conversation_time
  ON traces(conversation_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_traces_owner_time
  ON traces(owner_id, created_at DESC);

-- Mode profiles and per-surface defaults
CREATE TABLE IF NOT EXISTS profiles (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id TEXT NOT NULL,
  profile_name TEXT NOT NULL,
  profile_version INTEGER NOT NULL,
  active BOOLEAN NOT NULL DEFAULT true,
  prompt_overlay TEXT NOT NULL DEFAULT '',
  retrieval_policy_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  routing_policy_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  response_style_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  safety_policy_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  tool_policy_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (owner_id, profile_name, profile_version)
);

CREATE INDEX IF NOT EXISTS idx_profiles_owner_name_active
  ON profiles(owner_id, profile_name, active, profile_version DESC);

CREATE TABLE IF NOT EXISTS surface_profile_defaults (
  owner_id TEXT NOT NULL,
  surface TEXT NOT NULL,
  client_id TEXT NOT NULL DEFAULT '',
  profile_name TEXT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (owner_id, surface, client_id)
);

-- Future compatibility hooks for tiering overlays
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
