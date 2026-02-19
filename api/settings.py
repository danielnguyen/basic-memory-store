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


    chat_temperature: float | None = Field(
        default=None,
        alias="CHAT_TEMPERATURE",
        description="Optional temperature for chat completions. If unset, the field is omitted (recommended for O-series).",
    )

    # --- Indexing policy ---
    index_user_questions: bool = Field(
        default=False,
        alias="INDEX_USER_QUESTIONS",
        description="If false, user messages ending in '?' are not embedded/indexed (reduces query-echo in retrieval).",
    )
    index_assistant_messages: bool = Field(
        default=False,
        alias="INDEX_ASSISTANT_MESSAGES",
        description="If true, assistant messages are embedded/indexed (can improve recall but may add noise).",
    )
    min_index_chars: int = Field(
        default=12,
        alias="MIN_INDEX_CHARS",
        ge=1,
        le=1000,
        description="Minimum character length required to embed/index a message (reduces low-signal noise like 'ok', 'ping').",
    )

    # --- Context / Retrieval tuning ---
    recent_turns: int = Field(default=10, alias="RECENT_TURNS", ge=0, le=100)
    retrieval_k: int = Field(default=8, alias="RETRIEVAL_K", ge=1, le=50)
    max_context_chars: int = Field(default=12000, alias="MAX_CONTEXT_CHARS", ge=1000, le=200000)

    # --- Artifact storage hooks (MVP-friendly; S3/MinIO integration can be added later) ---
    artifacts_object_prefix: str = Field(
        default="artifacts",
        alias="ARTIFACTS_OBJECT_PREFIX",
        description="Object key prefix used when constructing artifact object_uri values.",
    )
    artifacts_upload_base_url: str = Field(
        default="http://localhost:9000",
        alias="ARTIFACTS_UPLOAD_BASE_URL",
        description="Base URL used to construct upload/download URLs for artifact endpoints.",
    )
    artifacts_presign_ttl_s: int = Field(
        default=900,
        alias="ARTIFACTS_PRESIGN_TTL_S",
        ge=60,
        le=86400,
        description="TTL in seconds for artifact upload/download URL responses.",
    )
    object_store_enabled: bool = Field(
        default=False,
        alias="OBJECT_STORE_ENABLED",
        description="Enable real S3/MinIO presigned URLs for artifacts.",
    )
    object_store_endpoint: str = Field(
        default="http://127.0.0.1:16335",
        alias="OBJECT_STORE_ENDPOINT",
        description="S3-compatible endpoint URL, e.g. http://memory-minio:9000",
    )
    object_store_bucket: str = Field(
        default="memory-artifacts",
        alias="OBJECT_STORE_BUCKET",
        description="Object storage bucket name for artifact blobs.",
    )
    object_store_access_key: str = Field(
        default="minioadmin",
        alias="OBJECT_STORE_ACCESS_KEY",
        description="Object storage access key.",
    )
    object_store_secret_key: str = Field(
        default="minioadmin",
        alias="OBJECT_STORE_SECRET_KEY",
        description="Object storage secret key.",
    )
    object_store_region: str = Field(
        default="us-east-1",
        alias="OBJECT_STORE_REGION",
        description="Object storage region used for signing.",
    )
    object_store_presign_base_url: str | None = Field(
        default=None,
        alias="OBJECT_STORE_PRESIGN_BASE_URL",
        description="Optional public base URL used to rewrite presigned URL host/port.",
    )
    object_store_include_content_type_in_put_signature: bool = Field(
        default=True,
        alias="OBJECT_STORE_INCLUDE_CONTENT_TYPE_IN_PUT_SIGNATURE",
        description="If true, Content-Type is included in presigned PUT signature and must match client upload header.",
    )
    artifacts_max_size_bytes: int = Field(
        default=104857600,
        alias="ARTIFACTS_MAX_SIZE_BYTES",
        ge=1,
        description="Maximum allowed artifact size in bytes.",
    )
    artifacts_allowed_mime: str = Field(
        default="image/png,image/jpeg,image/webp,application/pdf,text/plain,text/markdown,application/json,application/zip",
        alias="ARTIFACTS_ALLOWED_MIME",
        description="Comma-separated allowed artifact MIME types.",
    )

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
