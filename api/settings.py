from __future__ import annotations

from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Environment configuration for Basic Memory Store.

    This service is designed to run behind Docker/Portainer, so all configuration
    comes from env vars. No magic config files.
    """

    # --- Auth ---
    memory_api_key: str = Field(..., alias="MEMORY_API_KEY", description="API key required in x-api-key header")

    # --- Postgres ---
    pg_dsn: str = Field(
        ...,
        alias="PG_DSN",
        description="Postgres DSN, e.g. postgresql://user:pass@host:5432/dbname",
    )

    # --- Qdrant ---
    qdrant_url: str = Field(
        ...,
        alias="QDRANT_URL",
        description="Qdrant base URL, e.g. http://memory-db-qdrant:6333",
    )
    qdrant_collection: str = Field(
        default="messages",
        alias="QDRANT_COLLECTION",
        description="Qdrant collection name for message vectors",
    )

    # --- LiteLLM ---
    litellm_base_url: str = Field(
        ...,
        alias="LITELLM_BASE_URL",
        description="LiteLLM base URL, e.g. http://litellm:4000",
    )
    litellm_api_key: str | None = Field(
        default=None,
        alias="LITELLM_API_KEY",
        description="Optional Bearer token for LiteLLM (only if you enabled auth there)",
    )

    # --- Models (LiteLLM model names) ---
    chat_model: str = Field(default="gpt-4o-mini", alias="CHAT_MODEL")
    embed_model: str = Field(default="text-embedding-3-small", alias="EMBED_MODEL")

    # --- Context / Retrieval tuning ---
    recent_turns: int = Field(default=10, alias="RECENT_TURNS", ge=0, le=100)
    retrieval_k: int = Field(default=8, alias="RETRIEVAL_K", ge=1, le=50)
    max_context_chars: int = Field(default=12000, alias="MAX_CONTEXT_CHARS", ge=1000, le=200000)

    # pydantic-settings v2 config
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

@lru_cache
def get_settings() -> Settings:
    return Settings()
