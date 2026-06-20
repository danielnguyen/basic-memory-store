-- Additive Cluster 9B migration: R21 manual episodes, links, and audit trail.

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
  derivation_version TEXT NOT NULL DEFAULT 'r21-m0-v1',
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
