-- Additive Cluster 5 migration: event ingest substrate for external events.

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
