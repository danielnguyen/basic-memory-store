from __future__ import annotations

import os


TEST_ENV_DEFAULTS = {
    "MEMORY_API_KEY": "testkey",
    "PG_DSN": "postgresql://test:test@127.0.0.1:1/test",
    "QDRANT_URL": "http://127.0.0.1:1",
    "LITELLM_BASE_URL": "http://127.0.0.1:1",
    "LITELLM_API_KEY": "testkey",
    "CHAT_MODEL": "test-chat",
    "EMBED_MODEL": "test-embed",
    "OBJECT_STORE_ENABLED": "false",
}

for name, value in TEST_ENV_DEFAULTS.items():
    os.environ.setdefault(name, value)
