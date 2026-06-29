from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


Role = Literal["user", "assistant", "system", "tool"]
RetrievalScope = Literal["conversation", "client", "owner"]
TimeWindow = Literal["7d", "30d", "90d", "all"]
RetrievalMode = Literal["recent", "balanced", "historical"]
RetrievalContractMode = Literal["augmented", "raw", "compare"]
RetrievalSourceType = Literal["message", "derived_text"]
RetrievalEvidenceRole = Literal["canonical", "derived"]
RetrievalSourceAvailability = Literal[
    "available",
    "missing",
    "malformed",
    "unavailable",
    "owner_mismatch",
    "not_applicable",
]
RetrievalFreshnessState = Literal[
    "active",
    "parked",
    "stale",
    "superseded",
    "corrected",
    "forgotten_or_demoted",
    "unknown_freshness",
]


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
    time_window: TimeWindow = Field(
        default="all",
        description="Time window control for retrieval candidate selection.",
        examples=["30d"],
    )
    retrieval_mode: RetrievalMode = Field(
        default="balanced",
        description="Retrieval mode controlling recency bias.",
        examples=["balanced"],
    )


# ---- Conversations ----

class ConversationCreateRequest(BaseModel):
    owner_id: str = Field(..., description="Principal who owns this memory space.", examples=["owner_123"])
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
    owner_id: str = Field(..., examples=["owner_123"])
    client_id: Optional[str] = Field(default=None, examples=["car"])
    title: Optional[str] = Field(default=None, description="Optional title for newly created conversations.")
    idle_ttl_s: int = Field(default=1800, ge=60, le=86400, description="Reuse convo if active within this TTL (seconds).")


class ConversationResolveResponse(BaseModel):
    conversation_id: str
    reused: bool


# ---- Messages ----

class MessageCreateRequest(BaseModel):
    owner_id: str = Field(..., examples=["owner_123"])
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
    owner_id: str = Field(..., examples=["owner_123"])
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


# ---- Retrieval bundle ----

class RetrieveBundleRequest(BaseModel):
    request_id: str
    owner_id: str
    query: str
    mode: RetrievalContractMode = Field(
        default="augmented",
        description="Retrieval contract mode: augmented, raw canonical-only, or structural compare.",
    )
    retrieval: Optional[RetrievalOptions] = None
    include_artifacts: Optional[bool] = None
    allowed_memory_domains: Optional[List[str]] = Field(default=None, max_length=16)
    blocked_memory_domains: Optional[List[str]] = Field(default=None, max_length=16)


class RetrievalSourceRef(BaseModel):
    ref_type: RetrievalSourceType
    ref_id: str


class RetrievalPolicyMetadata(BaseModel):
    memory_domains: List[str] = Field(default_factory=list)
    sensitivity: Optional[str] = None


class DerivedProvenance(BaseModel):
    derived_id: str
    owner_id: str
    derivation_type: str
    source_refs: List[Dict[str, Any]] = Field(default_factory=list, max_length=50)
    derivation_version: str
    created_at: str
    status: str
    effective_status: Optional[str] = None
    confidence: Optional[float] = None
    explanation: Optional[str] = Field(default=None, max_length=500)
    generation_trace_id: Optional[str] = Field(default=None, max_length=160)
    compatibility_defaults: List[str] = Field(default_factory=list)
    provenance_status: Literal["complete"] = "complete"
    retrieval_reason: Optional[str] = Field(default=None, max_length=160)


class ArtifactRef(BaseModel):
    artifact_id: str
    owner_id: Optional[str] = None
    evidence_role: RetrievalEvidenceRole = "derived"
    file_path: str
    snippet: str
    relevance_score: Optional[float] = None
    repo_name: Optional[str] = None
    score_details: Dict[str, Any] = Field(default_factory=dict)
    source_ref: RetrievalSourceRef
    source_availability: RetrievalSourceAvailability = "unavailable"
    source_checks: List[Dict[str, Any]] = Field(default_factory=list, max_length=50)
    qualification_reasons: List[str] = Field(default_factory=list, max_length=16)
    memory_id: Optional[str] = None
    policy_metadata: RetrievalPolicyMetadata = Field(default_factory=RetrievalPolicyMetadata)
    freshness_state: RetrievalFreshnessState = "unknown_freshness"
    durable_status: Optional[str] = None
    last_verified_at: Optional[str] = None
    source_kind: Optional[str] = None
    confidence: Optional[float] = None
    supersedes: Optional[str] = None
    superseded_by: Optional[str] = None
    provenance: Optional[DerivedProvenance] = None


class RetrievalMessageItem(BaseModel):
    message_id: str
    owner_id: Optional[str] = None
    evidence_role: RetrievalEvidenceRole = "canonical"
    conversation_id: str
    role: Role
    content: str
    created_at: str
    score: Optional[float] = None
    score_details: Dict[str, Any] = Field(default_factory=dict)
    source_ref: RetrievalSourceRef
    source_availability: RetrievalSourceAvailability = "not_applicable"
    source_checks: List[Dict[str, Any]] = Field(default_factory=list, max_length=50)
    qualification_reasons: List[str] = Field(default_factory=list, max_length=16)
    memory_id: Optional[str] = None
    policy_metadata: RetrievalPolicyMetadata = Field(default_factory=RetrievalPolicyMetadata)
    freshness_state: RetrievalFreshnessState = "unknown_freshness"
    durable_status: Optional[str] = None
    last_verified_at: Optional[str] = None
    source_kind: Optional[str] = None
    confidence: Optional[float] = None
    supersedes: Optional[str] = None
    superseded_by: Optional[str] = None


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
    retrieval_debug: Dict[str, Any] = Field(default_factory=dict)


class RetrieveBundleResponse(BaseModel):
    request_id: str
    conversation_id: str
    bundle: RetrievalBundle
    raw_bundle: Optional[RetrievalBundle] = None
    augmented_bundle: Optional[RetrievalBundle] = None
    comparison: Optional[Dict[str, Any]] = None
    diagnostics: Dict[str, Any] = Field(default_factory=dict)


class DerivedInspectionResponse(BaseModel):
    derivative_class: Literal["derived_text", "proactive_suggestion", "memory_item", "episode"]
    contract: DerivedProvenance


DerivedClass = Literal["derived_text", "proactive_suggestion", "memory_item", "episode"]
DerivedInvalidationReason = Literal[
    "source_changed",
    "source_missing",
    "source_access_lost",
    "derivation_version_changed",
    "explicit_retraction",
    "existing_lifecycle_conflict",
]
DerivedReplayResult = Literal["identical", "replaced", "unsupported", "failed"]


class DerivedInvalidationRequest(BaseModel):
    request_id: str = Field(..., min_length=1, max_length=160)
    owner_id: str = Field(..., min_length=1, max_length=160)
    reason_code: DerivedInvalidationReason
    metadata: Dict[str, Any] = Field(default_factory=dict, max_length=8)
    source_ref: Optional[Dict[str, Any]] = None
    derivation_version: Optional[str] = Field(default=None, max_length=160)


class DerivedReplayRequest(BaseModel):
    request_id: str = Field(..., min_length=1, max_length=160)
    owner_id: str = Field(..., min_length=1, max_length=160)
    requested_derivation_version: Optional[str] = Field(default=None, max_length=160)
    expected_current_derivation_version: Optional[str] = Field(default=None, max_length=160)
    persist_replacement: bool = False


class DerivedLifecycleInspection(BaseModel):
    derived_class: DerivedClass
    derived_id: str
    owner_id: str
    contract: DerivedProvenance
    rebuildability: Literal["rebuildable", "replay_only", "not_rebuildable"]
    rebuildability_reason: str
    lifecycle_status: str
    invalidation: Dict[str, Any] = Field(default_factory=dict)
    source_summary: Dict[str, Any] = Field(default_factory=dict)
    events: List[Dict[str, Any]] = Field(default_factory=list)
    structural_snapshot: Dict[str, Any] = Field(default_factory=dict)


class DerivedInvalidationResponse(BaseModel):
    request_id: str
    changed: bool
    inspection: DerivedLifecycleInspection


class DerivedReplayResponse(BaseModel):
    request_id: str
    inspection: DerivedLifecycleInspection
    replay: Dict[str, Any] = Field(default_factory=dict)


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


# ---- Event ingest ----

class EventEntityIn(BaseModel):
    entity_type: str
    canonical_name: str
    metadata: Dict[str, Any] = Field(default_factory=dict)


class EventIngestRequest(BaseModel):
    request_id: str
    owner_id: str
    source_type: str
    source_event_id: str
    event_type: str
    event_time: Optional[datetime] = None
    payload_json: Dict[str, Any] = Field(default_factory=dict)
    entities: List[EventEntityIn] = Field(default_factory=list)


class EventIngestResponse(BaseModel):
    request_id: str
    created: bool
    event_log_id: str
    conversation_id: Optional[str] = None
    message_id: Optional[str] = None
    entity_ids: List[str] = Field(default_factory=list)


# ---- Proactive ----

SuggestionStatus = Literal["pending", "dismissed", "accepted", "expired"]
DeliveryStatus = Literal["not_attempted", "delivered", "failed"]
FeedbackType = Literal["dismissed", "useful", "not_useful", "accepted"]
ProactiveSurface = Literal["telegram"]


class ProactivePrefsUpdateRequest(BaseModel):
    owner_id: str
    enabled: bool
    allowed_surfaces_json: List[ProactiveSurface] = Field(default_factory=list)
    rule_prefs_json: Dict[str, Any] = Field(default_factory=dict)


class ProactivePrefsResponse(BaseModel):
    owner_id: str
    enabled: bool
    allowed_surfaces_json: List[str] = Field(default_factory=list)
    rule_prefs_json: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class ProactiveSuggestionItem(BaseModel):
    suggestion_id: str
    owner_id: str
    source_event_log_id: Optional[str] = None
    source_type: str
    kind: str
    status: SuggestionStatus
    title: str
    body: str
    explanation_json: Dict[str, Any] = Field(default_factory=dict)
    evidence_json: Dict[str, Any] = Field(default_factory=dict)
    target_surface: Optional[str] = None
    delivery_surface: Optional[str] = None
    delivery_status: DeliveryStatus
    delivery_external_id: Optional[str] = None
    delivery_error: Optional[str] = None
    delivered_at: Optional[str] = None
    created_at: str
    updated_at: str


class ProactiveSuggestionListResponse(BaseModel):
    suggestions: List[ProactiveSuggestionItem] = Field(default_factory=list)


class ProactiveSuggestionFeedbackRequest(BaseModel):
    owner_id: str
    feedback_type: FeedbackType
    reason: Optional[str] = None


class ProactiveSuggestionFeedbackResponse(BaseModel):
    feedback_id: str
    suggestion_id: str
    owner_id: str
    feedback_type: FeedbackType
    reason: Optional[str] = None
    status: SuggestionStatus
    created_at: str


class ProactiveDeliveryAttemptRequest(BaseModel):
    owner_id: str
    surface: ProactiveSurface
    status: Literal["delivered", "failed"]
    external_id: Optional[str] = None
    error: Optional[str] = None


class ProactiveDeliveryAttemptResponse(BaseModel):
    suggestion_id: str
    owner_id: str
    status: SuggestionStatus
    delivery_status: DeliveryStatus
    delivery_surface: Optional[str] = None
    delivery_external_id: Optional[str] = None
    delivery_error: Optional[str] = None
    delivered_at: Optional[str] = None
    updated_at: str


class ProactiveEvaluateRequest(BaseModel):
    request_id: str
    owner_id: str
    event_log_id: str
    surface: Optional[str] = None


class ProactiveEvaluateResponse(BaseModel):
    request_id: str
    owner_id: str
    event_log_id: str
    created_count: int
    suggestions: List[ProactiveSuggestionItem] = Field(default_factory=list)


# ---- Initiative ----

InitiativeDecisionStatus = Literal["created", "suppressed", "no_op"]


class InitiativeDecisionItem(BaseModel):
    decision_id: str
    initiative_event_id: str
    owner_id: str
    proactive_suggestion_id: Optional[str] = None
    decision_status: InitiativeDecisionStatus
    score: Optional[float] = None
    reason_json: Dict[str, Any] = Field(default_factory=dict)
    delivery_surface: Optional[str] = None
    delivery_status: DeliveryStatus
    suppression_reason: Optional[str] = None
    cooldown_identity_key: Optional[str] = None
    normalized_subject: Optional[str] = None
    cooldown_until: Optional[str] = None
    created_at: str


class InitiativeEventItem(BaseModel):
    initiative_event_id: str
    owner_id: str
    request_id: str
    source_event_log_id: Optional[str] = None
    trigger_type: str
    trigger_ref_json: Dict[str, Any] = Field(default_factory=dict)
    payload_json: Dict[str, Any] = Field(default_factory=dict)
    created_at: str


class InitiativeEvaluateRequest(BaseModel):
    request_id: str
    owner_id: str
    event_log_id: str
    surface: Optional[str] = None


class InitiativeEvaluateResponse(BaseModel):
    request_id: str
    owner_id: str
    event_log_id: str
    initiative_event: Optional[InitiativeEventItem] = None
    decisions: List[InitiativeDecisionItem] = Field(default_factory=list)
    suggestions: List[ProactiveSuggestionItem] = Field(default_factory=list)
    created_count: int


class InitiativeFeedbackRequest(BaseModel):
    owner_id: str
    decision_id: str
    feedback_type: FeedbackType
    feedback_json: Dict[str, Any] = Field(default_factory=dict)


class InitiativeFeedbackResponse(BaseModel):
    feedback_id: str
    decision_id: str
    proactive_feedback_id: Optional[str] = None
    owner_id: str
    feedback_type: FeedbackType
    feedback_json: Dict[str, Any] = Field(default_factory=dict)
    created_at: str


class InitiativeDetailResponse(BaseModel):
    initiative_event: InitiativeEventItem
    decisions: List[InitiativeDecisionItem] = Field(default_factory=list)


class InitiativeDebugResponse(BaseModel):
    request_id: str
    initiative_event: Optional[InitiativeEventItem] = None
    decisions: List[InitiativeDecisionItem] = Field(default_factory=list)
    suggestions: List[ProactiveSuggestionItem] = Field(default_factory=list)
    feedback: List[InitiativeFeedbackResponse] = Field(default_factory=list)


# ---- Hygiene / Graph ----

class HygieneScanRequest(BaseModel):
    owner_id: str
    limit: int = Field(default=50, ge=1, le=500)


class HygieneFlagItem(BaseModel):
    flag_id: str
    owner_id: str
    subject_type: str
    subject_id: Optional[str] = None
    flag_type: str
    details: Dict[str, Any] = Field(default_factory=dict)
    status: str
    created_at: str
    resolved_at: Optional[str] = None


class HygieneScanResponse(BaseModel):
    owner_id: str
    flags_created: int
    flags: List[HygieneFlagItem] = Field(default_factory=list)


class HygieneFlagListResponse(BaseModel):
    flags: List[HygieneFlagItem] = Field(default_factory=list)


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


# ---- Memory promotion ----

MemoryEventType = Literal[
    "created",
    "updated",
    "reinforced",
    "superseded",
    "expired",
    "promoted",
    "suppressed",
    "decayed",
    "state_changed",
]
MemoryDurableStatus = Literal[
    "active",
    "parked",
    "stale",
    "contradicted",
    "corrected",
    "invalidated",
    "superseded",
    "expired",
    "retracted",
    "forgotten_or_demoted",
    "rebuilding",
]


class MemorySourceRef(BaseModel):
    ref_type: str = Field(..., min_length=1, max_length=64)
    ref_id: str = Field(..., min_length=1, max_length=160)
    support_kind: str = Field(default="direct", min_length=1, max_length=64)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class MemoryItemResponse(BaseModel):
    memory_id: str
    owner_id: str
    memory_type: str
    summary: str
    source_refs: List[Dict[str, Any]] = Field(default_factory=list)
    source_ref_hash: str
    scores: Dict[str, Any] = Field(default_factory=dict)
    promotion_state: str
    status: str
    freshness_state: RetrievalFreshnessState
    supersedes_memory_id: Optional[str] = None
    superseded_by_memory_id: Optional[str] = None
    last_reinforced_at: Optional[str] = None
    expires_at: Optional[str] = None
    derivation_version: str
    confidence: Optional[float] = None
    explanation: Dict[str, Any] = Field(default_factory=dict)
    generation_trace_id: Optional[str] = None
    created_at: str
    updated_at: str


class MemoryPromoteRequest(BaseModel):
    request_id: str = Field(..., min_length=1, max_length=160)
    owner_id: str = Field(..., min_length=1, max_length=160)
    memory_type: str = Field(..., min_length=1, max_length=64)
    summary: str = Field(..., min_length=1, max_length=4000)
    source_refs: List[MemorySourceRef] = Field(..., min_length=1, max_length=50)
    scores: Dict[str, Any] = Field(default_factory=dict)
    promotion_state: Literal["promoted"] = "promoted"
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    explanation: Dict[str, Any] = Field(default_factory=dict)
    generation_trace_id: Optional[str] = Field(default=None, max_length=160)
    expires_at: Optional[datetime] = None
    reinforce: bool = False
    supersedes_memory_id: Optional[str] = None


class MemoryPromoteResponse(BaseModel):
    request_id: str
    memory: MemoryItemResponse
    created: bool
    updated: bool
    reinforced: bool
    superseded: bool
    events_appended: List[MemoryEventType] = Field(default_factory=list)


class MemoryReinforceRequest(BaseModel):
    request_id: str = Field(..., min_length=1, max_length=160)
    owner_id: str = Field(..., min_length=1, max_length=160)
    scores: Dict[str, Any] = Field(default_factory=dict)
    reason: Dict[str, Any] = Field(default_factory=dict)


class MemoryTransitionReason(BaseModel):
    code: str = Field(..., min_length=1, max_length=64)
    metadata: Dict[str, Any] = Field(default_factory=dict, max_length=8)


class MemoryTransitionRequest(BaseModel):
    request_id: str = Field(..., min_length=1, max_length=160)
    owner_id: str = Field(..., min_length=1, max_length=160)
    status: MemoryDurableStatus
    reason: MemoryTransitionReason
    related_memory_id: Optional[str] = None


class MemoryTransitionResponse(BaseModel):
    request_id: str
    changed: bool
    memory: MemoryItemResponse
    events_appended: List[MemoryEventType] = Field(default_factory=list)


class MemoryEventItem(BaseModel):
    event_id: str
    memory_id: str
    owner_id: str
    event_type: MemoryEventType
    reason: Dict[str, Any] = Field(default_factory=dict)
    created_at: str


class MemoryDebugResponse(BaseModel):
    memory: MemoryItemResponse
    events: List[MemoryEventItem] = Field(default_factory=list)


# ---- Episodes ----

EpisodeEventType = Literal["created", "updated", "linked"]


class EpisodeSourceRef(BaseModel):
    ref_type: str = Field(..., min_length=1, max_length=64)
    ref_id: str = Field(..., min_length=1, max_length=160)
    support_kind: str = Field(default="direct", min_length=1, max_length=64)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class EpisodeItemResponse(BaseModel):
    episode_id: str
    owner_id: str
    title: str
    summary: str
    episode_type: str
    trigger: Dict[str, Any] = Field(default_factory=dict)
    outcome: Optional[str] = None
    significance: Optional[str] = None
    unresolved: Dict[str, Any] = Field(default_factory=dict)
    source_refs: List[Dict[str, Any]] = Field(default_factory=list)
    source_ref_hash: str
    episode_key: str
    callback_candidates: List[Any] = Field(default_factory=list)
    time_window: Dict[str, Any] = Field(default_factory=dict)
    participants: List[Any] = Field(default_factory=list)
    status: str
    derivation_version: str
    confidence: Optional[float] = None
    explanation: Dict[str, Any] = Field(default_factory=dict)
    generation_trace_id: Optional[str] = None
    created_at: str
    updated_at: str


class EpisodeCreateRequest(BaseModel):
    request_id: str = Field(..., min_length=1, max_length=160)
    owner_id: str = Field(..., min_length=1, max_length=160)
    title: str = Field(..., min_length=1, max_length=400)
    summary: str = Field(..., min_length=1, max_length=4000)
    episode_type: str = Field(..., min_length=1, max_length=64)
    source_refs: List[EpisodeSourceRef] = Field(..., min_length=1, max_length=50)
    trigger: Dict[str, Any] = Field(default_factory=dict)
    outcome: Optional[str] = Field(default=None, max_length=4000)
    significance: Optional[str] = Field(default=None, max_length=4000)
    unresolved: Dict[str, Any] = Field(default_factory=dict)
    callback_candidates: List[Any] = Field(default_factory=list)
    time_window: Dict[str, Any] = Field(default_factory=dict)
    participants: List[Any] = Field(default_factory=list)
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    explanation: Dict[str, Any] = Field(default_factory=dict)
    generation_trace_id: Optional[str] = Field(default=None, max_length=160)


class EpisodeCreateResponse(BaseModel):
    request_id: str
    episode: EpisodeItemResponse
    created: bool
    updated: bool


class EpisodeLinkIn(BaseModel):
    ref_type: str = Field(..., min_length=1, max_length=64)
    ref_id: str = Field(..., min_length=1, max_length=160)
    relationship: str = Field(..., min_length=1, max_length=64)


class EpisodeLinkItem(BaseModel):
    link_id: str
    episode_id: str
    owner_id: str
    ref_type: str
    ref_id: str
    relationship: str
    created_at: str


class EpisodeLinkRequest(BaseModel):
    request_id: str = Field(..., min_length=1, max_length=160)
    owner_id: str = Field(..., min_length=1, max_length=160)
    links: List[EpisodeLinkIn] = Field(..., min_length=1, max_length=50)


class EpisodeLinkResponse(BaseModel):
    request_id: str
    episode_id: str
    created_count: int
    existing_count: int
    links: List[EpisodeLinkItem] = Field(default_factory=list)


class EpisodeEventItem(BaseModel):
    event_id: str
    episode_id: str
    owner_id: str
    event_type: EpisodeEventType
    reason: Dict[str, Any] = Field(default_factory=dict)
    created_at: str


class EpisodeDebugResponse(BaseModel):
    episode: EpisodeItemResponse
    links: List[EpisodeLinkItem] = Field(default_factory=list)
    events: List[EpisodeEventItem] = Field(default_factory=list)


# ---- Recall selection ----

RecallCandidateType = Literal["memory_item", "episode", "message", "artifact", "event", "derived_text"]
RecallDecisionValue = Literal["mention", "suppress", "implicit_only"]
RecallMentionStrategy = Literal["none", "implicit", "light_callback", "explicit_callback"]


class RecallSourceRef(BaseModel):
    ref_type: str = Field(..., min_length=1, max_length=64)
    ref_id: str = Field(..., min_length=1, max_length=160)
    support_kind: str = Field(default="direct", min_length=1, max_length=64)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class RecallContext(BaseModel):
    scene_id: Optional[str] = Field(default=None, max_length=160)
    surface: Optional[str] = Field(default=None, max_length=80)
    urgency: Optional[str] = Field(default=None, max_length=80)
    sensitivity: Optional[str] = Field(default=None, max_length=80)


class RecallCandidate(BaseModel):
    candidate_id: str = Field(..., min_length=1, max_length=160)
    candidate_type: RecallCandidateType
    title: Optional[str] = Field(default=None, max_length=400)
    summary: Optional[str] = Field(default=None, max_length=4000)
    source_refs: List[RecallSourceRef] = Field(default_factory=list, max_length=50)
    relevance_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    salience_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    recency_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class RecallSelectRequest(BaseModel):
    request_id: str = Field(..., min_length=1, max_length=160)
    owner_id: str = Field(..., min_length=1, max_length=160)
    context: RecallContext = Field(default_factory=RecallContext)
    candidates: List[RecallCandidate] = Field(..., min_length=1, max_length=100)


class RecallDecisionItem(BaseModel):
    id: str
    request_id: str
    owner_id: str
    candidate_id: str
    candidate_type: RecallCandidateType
    candidate_ref: Dict[str, Any] = Field(default_factory=dict)
    source_refs: List[Dict[str, Any]] = Field(default_factory=list)
    context: Dict[str, Any] = Field(default_factory=dict)
    relevance_score: Optional[float] = None
    salience_score: Optional[float] = None
    recency_score: Optional[float] = None
    mentionability_score: float
    decision: RecallDecisionValue
    mention_strategy: RecallMentionStrategy
    prompt_eligible: bool
    reason: Dict[str, Any] = Field(default_factory=dict)
    created_at: str


class RecallSelectResponse(BaseModel):
    request_id: str
    owner_id: str
    decision_count: int
    decisions: List[RecallDecisionItem] = Field(default_factory=list)


class RecallDebugResponse(BaseModel):
    request_id: str
    owner_id: str
    context: Dict[str, Any] = Field(default_factory=dict)
    decision_count: int
    decisions: List[RecallDecisionItem] = Field(default_factory=list)


# ---- Traces ----

class TraceCreateRequest(BaseModel):
    request_id: str
    conversation_id: str
    owner_id: str
    client_id: Optional[str] = None
    surface: str
    profile: Dict[str, Any]
    retrieval: Dict[str, Any]
    prompt: Dict[str, Any] = Field(default_factory=dict)
    router_decision: Dict[str, Any]
    manual_override: Dict[str, Any] = Field(default_factory=dict)
    model_call: Dict[str, Any]
    model_calls: List[Dict[str, Any]] = Field(default_factory=list)
    fallback: Dict[str, Any] = Field(default_factory=dict)
    artifacts: Dict[str, Any] = Field(default_factory=dict)
    references: List[Dict[str, Any]] = Field(default_factory=list)
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
    prompt: Dict[str, Any] = Field(default_factory=dict)
    router_decision: Dict[str, Any]
    manual_override: Dict[str, Any]
    model_call: Dict[str, Any]
    model_calls: List[Dict[str, Any]] = Field(default_factory=list)
    fallback: Dict[str, Any]
    artifacts: Dict[str, Any] = Field(default_factory=dict)
    references: List[Dict[str, Any]] = Field(default_factory=list)
    cost: Dict[str, Any]
    latency_ms: Optional[int] = None
    status: str
    error: Optional[str] = None
    created_at: str


# ---- Chat ----

class ChatRequest(BaseModel):
    owner_id: str = Field(..., examples=["owner_123"])
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


InteractionMode = Literal["text", "voice_mediated"]
LatencyPreference = Literal["normal", "low", "lowest"]
VerbosityTarget = Literal["short", "normal", "detailed"]
OutputFormat = Literal["plain_text", "markdown", "speech"]


class SurfaceContext(BaseModel):
    surface_type: Optional[str] = Field(default=None, min_length=1, max_length=80)
    interaction_mode: Optional[InteractionMode] = None
    spoken_output: Optional[bool] = None
    active_task_mode: Optional[bool] = None
    latency_preference: Optional[LatencyPreference] = None
    verbosity_target: Optional[VerbosityTarget] = None
    allows_expansion: Optional[bool] = None
    output_format: Optional[OutputFormat] = None
    style_envelope: Dict[str, Any] = Field(default_factory=dict)


class OrchestrateChatRequest(ChatRequest):
    surface: str = "unknown"
    artifact_ids: Optional[List[str]] = None
    surface_context: Optional[SurfaceContext] = None


class OrchestrateChatResponse(ChatResponse):
    request_id: str
