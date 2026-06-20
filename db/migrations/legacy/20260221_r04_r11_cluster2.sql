-- Cluster 2: traces/profiles contract alignment

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- traces table alignment (one trace doc per request)
ALTER TABLE traces DROP COLUMN IF EXISTS trace_id;
ALTER TABLE traces ADD COLUMN IF NOT EXISTS client_id TEXT;
ALTER TABLE traces ADD COLUMN IF NOT EXISTS profile_json JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE traces ADD COLUMN IF NOT EXISTS manual_override_json JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE traces ADD COLUMN IF NOT EXISTS model_call_json JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE traces ADD COLUMN IF NOT EXISTS fallback_json JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE traces ADD COLUMN IF NOT EXISTS status TEXT;
ALTER TABLE traces ADD COLUMN IF NOT EXISTS error_text TEXT;

-- Backfill + constraints for fresh contract
UPDATE traces SET model_call_json = COALESCE(model_calls_json, '{}'::jsonb)
WHERE model_call_json = '{}'::jsonb;

ALTER TABLE traces DROP COLUMN IF EXISTS model_calls_json;
ALTER TABLE traces ALTER COLUMN profile_json SET DEFAULT '{}'::jsonb;
ALTER TABLE traces ALTER COLUMN retrieval_json SET DEFAULT '{}'::jsonb;
ALTER TABLE traces ALTER COLUMN router_decision_json SET DEFAULT '{}'::jsonb;
ALTER TABLE traces ALTER COLUMN manual_override_json SET DEFAULT '{}'::jsonb;
ALTER TABLE traces ALTER COLUMN model_call_json SET DEFAULT '{}'::jsonb;
ALTER TABLE traces ALTER COLUMN fallback_json SET DEFAULT '{}'::jsonb;
ALTER TABLE traces ALTER COLUMN cost_json SET DEFAULT '{}'::jsonb;
ALTER TABLE traces ALTER COLUMN surface SET NOT NULL;
ALTER TABLE traces ALTER COLUMN owner_id SET NOT NULL;
ALTER TABLE traces ALTER COLUMN conversation_id SET NOT NULL;
UPDATE traces SET status = 'ok' WHERE status IS NULL;
ALTER TABLE traces ALTER COLUMN status SET NOT NULL;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'traces_status_check'
  ) THEN
    ALTER TABLE traces
      ADD CONSTRAINT traces_status_check CHECK (status IN ('ok', 'degraded', 'failed'));
  END IF;
END $$;

-- profiles
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

-- surface profile defaults
CREATE TABLE IF NOT EXISTS surface_profile_defaults (
  owner_id TEXT NOT NULL,
  surface TEXT NOT NULL,
  client_id TEXT NOT NULL DEFAULT '',
  profile_name TEXT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (owner_id, surface, client_id)
);

ALTER TABLE surface_profile_defaults
  ALTER COLUMN client_id SET DEFAULT '';
UPDATE surface_profile_defaults SET client_id = '' WHERE client_id IS NULL;
ALTER TABLE surface_profile_defaults
  ALTER COLUMN client_id SET NOT NULL;
