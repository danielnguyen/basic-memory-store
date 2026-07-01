-- Basic Memory Store schema baseline for fresh installs and explicit adoption.
-- Existing installations advance through forward-only managed migrations.

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
  policy_metadata JSONB,
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
  policy_metadata JSONB,
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

CREATE TABLE IF NOT EXISTS event_ingest_log (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id TEXT NOT NULL,
  source_type TEXT NOT NULL,
  source_event_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  event_time TIMESTAMPTZ,
  payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  conversation_id UUID REFERENCES conversations(id) ON DELETE SET NULL,
  message_id UUID REFERENCES messages(id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (owner_id, source_type, source_event_id)
);

CREATE INDEX IF NOT EXISTS idx_event_ingest_log_owner_source_time
  ON event_ingest_log(owner_id, source_type, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_event_ingest_log_conversation
  ON event_ingest_log(conversation_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_event_ingest_log_message
  ON event_ingest_log(message_id, created_at DESC);

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
  policy_metadata JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pinned_memories_owner_time
  ON pinned_memories(owner_id, created_at DESC);

CREATE TABLE IF NOT EXISTS policy_overlays (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id TEXT NOT NULL,
  surface TEXT,
  policy_json JSONB NOT NULL,
  policy_metadata JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_policy_overlays_owner_surface
  ON policy_overlays(owner_id, surface, created_at DESC);

CREATE TABLE IF NOT EXISTS persona_overlays (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id TEXT NOT NULL,
  surface TEXT,
  persona_json JSONB NOT NULL,
  policy_metadata JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_persona_overlays_owner_surface
  ON persona_overlays(owner_id, surface, created_at DESC);

-- Cluster 4 additive scaffolding: hygiene + graph
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

-- Memory promotion and audit events.
CREATE TABLE IF NOT EXISTS memory_items (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id TEXT NOT NULL,
  memory_type TEXT NOT NULL,
  summary TEXT NOT NULL,
  source_refs_json JSONB NOT NULL DEFAULT '[]'::jsonb,
  source_ref_hash TEXT NOT NULL,
  scores_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  promotion_state TEXT NOT NULL DEFAULT 'promoted' CHECK (promotion_state IN ('candidate', 'promoted', 'suppressed', 'decayed')),
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN (
    'active',
    'parked',
    'stale',
    'contradicted',
    'corrected',
    'invalidated',
    'superseded',
    'expired',
    'retracted',
    'forgotten_or_demoted',
    'rebuilding'
  )),
  supersedes_memory_id UUID REFERENCES memory_items(id) ON DELETE SET NULL,
  superseded_by_memory_id UUID REFERENCES memory_items(id) ON DELETE SET NULL,
  last_reinforced_at TIMESTAMPTZ,
  expires_at TIMESTAMPTZ,
  derivation_version TEXT NOT NULL DEFAULT 'memory-promotion-v1',
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

-- Episodes, links, and audit events.
CREATE TABLE IF NOT EXISTS episodes (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id TEXT NOT NULL,
  title TEXT NOT NULL,
  summary TEXT NOT NULL,
  episode_type TEXT NOT NULL,
  trigger_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  outcome TEXT,
  significance TEXT,
  unresolved_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  source_refs_json JSONB NOT NULL DEFAULT '[]'::jsonb,
  source_ref_hash TEXT NOT NULL,
  episode_key TEXT NOT NULL,
  callback_candidates_json JSONB NOT NULL DEFAULT '[]'::jsonb,
  time_window_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  participants_json JSONB NOT NULL DEFAULT '[]'::jsonb,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'stale', 'invalidated', 'superseded', 'expired')),
  derivation_version TEXT NOT NULL DEFAULT 'episode-construction-v1',
  confidence DOUBLE PRECISION CHECK (confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)),
  explanation_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  generation_trace_id TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_episodes_owner_episode_key_active
  ON episodes(owner_id, episode_key)
  WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_episodes_owner_status_time
  ON episodes(owner_id, status, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_episodes_owner_source_ref_hash
  ON episodes(owner_id, source_ref_hash, created_at DESC);

CREATE TABLE IF NOT EXISTS episode_links (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  episode_id UUID NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
  owner_id TEXT NOT NULL,
  ref_type TEXT NOT NULL,
  ref_id TEXT NOT NULL,
  relationship TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_episode_links_episode_ref_relationship
  ON episode_links(episode_id, ref_type, ref_id, relationship);

CREATE INDEX IF NOT EXISTS idx_episode_links_owner_time
  ON episode_links(owner_id, created_at DESC);

CREATE TABLE IF NOT EXISTS episode_events (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  episode_id UUID NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
  owner_id TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK (event_type IN ('created', 'updated', 'linked')),
  reason_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_episode_events_episode_time
  ON episode_events(episode_id, created_at ASC);

CREATE INDEX IF NOT EXISTS idx_episode_events_owner_type_time
  ON episode_events(owner_id, event_type, created_at DESC);

-- Recall selection and mentionability decisions.
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
