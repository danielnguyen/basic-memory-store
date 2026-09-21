CREATE TABLE IF NOT EXISTS work_items (
  work_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id TEXT NOT NULL CHECK (
    char_length(owner_id) BETWEEN 1 AND 120 AND owner_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]*$'
  ),
  conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  request_id TEXT NOT NULL CHECK (
    char_length(request_id) BETWEEN 1 AND 120 AND request_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]*$'
  ),
  client_id TEXT CHECK (
    client_id IS NULL OR
    (char_length(client_id) BETWEEN 1 AND 120 AND client_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]*$')
  ),
  surface TEXT NOT NULL CHECK (
    char_length(surface) BETWEEN 1 AND 64 AND surface ~ '^[A-Za-z0-9][A-Za-z0-9._:-]*$'
  ),
  state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'running', 'completed', 'failed')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ,
  assistant_message_id UUID UNIQUE REFERENCES messages(id) ON DELETE RESTRICT,
  failure_code TEXT CHECK (
    failure_code IN ('interrupted', 'execution_failed', 'dependency_unavailable', 'authority_unavailable')
  ),
  UNIQUE (owner_id, request_id),
  CONSTRAINT work_items_state_fields_check CHECK (
    (state = 'pending' AND started_at IS NULL AND completed_at IS NULL
      AND assistant_message_id IS NULL AND failure_code IS NULL)
    OR (state = 'running' AND started_at IS NOT NULL AND completed_at IS NULL
      AND assistant_message_id IS NULL AND failure_code IS NULL)
    OR (state = 'completed' AND started_at IS NOT NULL AND completed_at IS NOT NULL
      AND assistant_message_id IS NOT NULL AND failure_code IS NULL)
    OR (state = 'failed' AND completed_at IS NOT NULL
      AND assistant_message_id IS NULL AND failure_code IS NOT NULL)
  ),
  CHECK (started_at IS NULL OR started_at >= created_at),
  CHECK (completed_at IS NULL OR completed_at >= COALESCE(started_at, created_at))
);

CREATE TABLE IF NOT EXISTS current_work (
  owner_id TEXT NOT NULL CHECK (
    char_length(owner_id) BETWEEN 1 AND 120 AND owner_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]*$'
  ),
  client_id TEXT NOT NULL CHECK (
    char_length(client_id) BETWEEN 1 AND 120 AND client_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]*$'
  ),
  work_id UUID NOT NULL REFERENCES work_items(work_id) ON DELETE CASCADE,
  PRIMARY KEY (owner_id, client_id)
);
