CREATE TABLE IF NOT EXISTS presence_surface_permissions (
    owner_id TEXT NOT NULL CHECK (length(owner_id) BETWEEN 1 AND 120 AND owner_id = btrim(owner_id)),
    surface TEXT NOT NULL CHECK (length(surface) BETWEEN 1 AND 64 AND surface ~ '^[a-z][a-z0-9_-]*$'),
    conversation_context_allowed BOOLEAN NOT NULL,
    proactive_presence_allowed BOOLEAN NOT NULL,
    ambient_listening_allowed BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_id, surface)
);
