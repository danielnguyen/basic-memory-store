from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


Role = Literal["user", "assistant", "system", "tool"]
RetrievalScope = Literal["conversation", "client", "owner"]


class MessageIn(BaseModel):
    role: Role = Field(..., description="Message role.", examples=["user"])
    content: str = Field(..., description="Message content.", examples=["Remember that my favorite snack is pretzels."])


class RetrievalOptions(BaseModel):
    k: int = Field(default=8, ge=1, le=50, description="Number of retrieved items to include.")
    min_score: float = Field(
        default=0.25,
        ge=0.0,
        le=1.0,
        description="Minimum cosine similarity threshold for vector search.",
    )
    scope: RetrievalScope = Field(
        default="conversation",
        description="Retrieval scope: conversation (default), client, or owner.",
        examples=["conversation"],
    )


# ---- Conversations ----

class ConversationCreateRequest(BaseModel):
    owner_id: str = Field(..., description="Principal who owns this memory space.", examples=["daniel"])
    client_id: Optional[str] = Field(default=None, description="Device/client source.", examples=["car"])
    title: Optional[str] = Field(default=None, description="Optional human title.", examples=["general chat"])


class ConversationCreateResponse(BaseModel):
    conversation_id: str = Field(..., description="UUID of the new conversation.")


class ConversationSummary(BaseModel):
    conversation_id: str
    owner_id: str
    client_id: Optional[str] = None
    title: Optional[str] = None
    created_at: str
    updated_at: str


class ConversationListResponse(BaseModel):
    conversations: List[ConversationSummary]
    next_cursor: Optional[str] = Field(
        default=None,
        description="Opaque cursor for pagination (pass back as cursor=...).",
    )


class ConversationResolveRequest(BaseModel):
    owner_id: str = Field(..., examples=["daniel"])
    client_id: Optional[str] = Field(default=None, examples=["car"])
    title: Optional[str] = Field(default=None, description="Optional title for newly created conversations.")
    idle_ttl_s: int = Field(default=1800, ge=60, le=86400, description="Reuse convo if active within this TTL (seconds).")


class ConversationResolveResponse(BaseModel):
    conversation_id: str
    reused: bool


# ---- Messages ----

class MessageCreateRequest(BaseModel):
    owner_id: str = Field(..., examples=["daniel"])
    role: Role = Field(..., examples=["user"])
    content: str = Field(..., examples=["Hello world"])
    client_id: Optional[str] = Field(default=None, examples=["phone"])
    metadata: Optional[Dict[str, Any]] = Field(default=None, description="Arbitrary JSON metadata.")


class MessageCreateResponse(BaseModel):
    message_id: str


# ---- Artifacts ----

class ArtifactInitRequest(BaseModel):
    owner_id: str
    client_id: Optional[str] = None
    conversation_id: Optional[str] = None
    filename: str
    mime: str
    size: int = Field(..., ge=1)
    source_surface: Optional[str] = None


class ArtifactInitResponse(BaseModel):
    artifact_id: str
    upload_url: str
    upload_url_expires_in_s: int
    object_uri: str
    status: str


class ArtifactCompleteRequest(BaseModel):
    artifact_id: str
    status: Literal["completed", "failed"] = "completed"
    sha256: Optional[str] = None


class ArtifactResponse(BaseModel):
    artifact_id: str
    owner_id: str
    client_id: Optional[str] = None
    conversation_id: Optional[str] = None
    filename: str
    mime: str
    size: int
    object_uri: str
    source_surface: Optional[str] = None
    status: str
    sha256: Optional[str] = None
    created_at: str
    completed_at: Optional[str] = None
    download_url: str
    download_url_expires_in_s: int


# ---- Retrieval (legacy) ----

class RetrieveRequest(BaseModel):
    owner_id: str = Field(..., examples=["daniel"])
    client_id: Optional[str] = Field(
        default=None,
        examples=["unit"],
        description="Optional client namespace for multi-client filtering.",
    )
    query: str = Field(..., examples=["favorite snack"])
    k: int = Field(default=8, ge=1, le=50)
    min_score: float = Field(
        default=0.25,
        ge=0.0,
        le=1.0,
        description="Minimum cosine similarity threshold.",
    )
    conversation_id: Optional[str] = Field(
        default=None,
        description="If set, restrict retrieval to a conversation.",
    )

    exclude_message_ids: Optional[List[str]] = Field(
        default=None,
        description="Optional list of message_ids to exclude from results (e.g., the query message itself).",
        examples=[["550e8400-e29b-41d4-a716-446655440000"]],
    )


class RetrieveHit(BaseModel):
    message_id: str
    conversation_id: str
    role: Role
    content: str
    created_at: str
    score: Optional[float] = Field(default=None, description="Vector similarity score (higher is better).")


class RetrieveResponse(BaseModel):
    hits: List[RetrieveHit]


# ---- Tiered retrieval (legacy/orchestrator wrapper) ----

class OverlayItem(BaseModel):
    id: str
    content: str
    metadata: Dict[str, Any] = Field(default_factory=dict)


class TieredRetrieveRequest(BaseModel):
    owner_id: str
    client_id: Optional[str] = None
    query: str
    surface: Optional[str] = None
    k: int = Field(default=8, ge=1, le=50)
    min_score: float = Field(default=0.25, ge=0.0, le=1.0)
    working_limit: int = Field(default=8, ge=1, le=100)
    pinned_limit: int = Field(default=5, ge=0, le=100)


class TieredRetrieveResponse(BaseModel):
    conversation_id: str
    query: str
    working: List[RetrieveHit]
    semantic: List[RetrieveHit]
    pinned: List[OverlayItem]
    policy: List[OverlayItem]
    persona: List[OverlayItem]


# ---- Retrieval bundle (R04/R11 MVP) ----

class RetrieveBundleRequest(BaseModel):
    request_id: str
    owner_id: str
    query: str
    retrieval: Optional[RetrievalOptions] = None
    include_artifacts: bool = False


class ArtifactRef(BaseModel):
    artifact_id: str
    file_path: str
    snippet: str
    relevance_score: Optional[float] = None
    repo_name: Optional[str] = None


class RetrievalMessageItem(BaseModel):
    message_id: str
    conversation_id: str
    role: Role
    content: str
    created_at: str
    score: Optional[float] = None


class ObservedMetadata(BaseModel):
    mime_types: List[str] = Field(default_factory=list)
    has_artifacts: bool = False
    has_code_like_content: bool = False
    estimated_chars: int = 0


class RetrievalBundle(BaseModel):
    recent: List[RetrievalMessageItem] = Field(default_factory=list)
    semantic: List[RetrievalMessageItem] = Field(default_factory=list)
    artifact_refs: List[ArtifactRef] = Field(default_factory=list)
    token_estimate_total: Optional[int] = None
    observed_metadata: ObservedMetadata


class RetrieveBundleResponse(BaseModel):
    request_id: str
    conversation_id: str
    bundle: RetrievalBundle


# ---- Ingestion ----

class FileIngestionRequest(BaseModel):
    owner_id: str
    client_id: Optional[str] = None
    source_surface: Optional[str] = None
    repo_name: Optional[str] = None
    paths: List[str] = Field(default_factory=list)


class FileIngestionResponse(BaseModel):
    ingestion_id: str
    owner_id: str
    repo_name: Optional[str] = None
    files_seen: int
    files_ingested: int
    chunks_created: int
    artifacts_created: int
    status: Literal["completed"]


# ---- Profiles ----

class ProfileResolveRequest(BaseModel):
    owner_id: str
    surface: str
    requested_profile: Optional[str] = None
    client_id: Optional[str] = None


class ProfileResolveResponse(BaseModel):
    profile_name: str
    source: Literal["requested", "surface_default", "global_default"]
    profile_version: int
    effective_profile_ref: str
    prompt_overlay: str
    retrieval_policy: Dict[str, Any]
    routing_policy: Dict[str, Any]
    response_style: Dict[str, Any]
    safety_policy: Dict[str, Any]
    tool_policy: Dict[str, Any]


# ---- Traces ----

class TraceCreateRequest(BaseModel):
    request_id: str
    conversation_id: str
    owner_id: str
    client_id: Optional[str] = None
    surface: str
    profile: Dict[str, Any]
    retrieval: Dict[str, Any]
    router_decision: Dict[str, Any]
    manual_override: Dict[str, Any] = Field(default_factory=dict)
    model_call: Dict[str, Any]
    fallback: Dict[str, Any] = Field(default_factory=dict)
    cost: Dict[str, Any] = Field(default_factory=dict)
    latency_ms: Optional[int] = None
    status: Literal["ok", "degraded", "failed"]
    error: Optional[str] = None


class TraceCreateResponse(BaseModel):
    trace_id: str
    request_id: str


class TraceResponse(BaseModel):
    trace_id: str
    request_id: str
    conversation_id: str
    owner_id: str
    client_id: Optional[str] = None
    surface: str
    profile: Dict[str, Any]
    retrieval: Dict[str, Any]
    router_decision: Dict[str, Any]
    manual_override: Dict[str, Any]
    model_call: Dict[str, Any]
    fallback: Dict[str, Any]
    cost: Dict[str, Any]
    latency_ms: Optional[int] = None
    status: str
    error: Optional[str] = None
    created_at: str


# ---- Chat ----

class ChatRequest(BaseModel):
    owner_id: str = Field(..., examples=["daniel"])
    conversation_id: Optional[str] = Field(
        default=None,
        description="If omitted, a new conversation is created automatically.",
        examples=["550e8400-e29b-41d4-a716-446655440000"],
    )
    client_id: Optional[str] = Field(default=None, examples=["car"])
    messages: List[MessageIn] = Field(..., description="New messages to process (usually one user message).")
    retrieval: Optional[RetrievalOptions] = Field(default=None)
    debug: bool = Field(
        default=False,
        description="If true, include retrieval diagnostics in the response."
    )


class RetrievalDebugHit(BaseModel):
    message_id: str
    score: float


class RetrievalDebug(BaseModel):
    scope_used: RetrievalScope
    fallback_used: bool
    hits: List[RetrievalDebugHit]


class ChatResponse(BaseModel):
    conversation_id: str
    answer: str
    retrieved_count: int
    debug: Optional[RetrievalDebug] = None


class OrchestrateChatRequest(ChatRequest):
    surface: str = "unknown"
    artifact_ids: Optional[List[str]] = None


class OrchestrateChatResponse(ChatResponse):
    request_id: str
