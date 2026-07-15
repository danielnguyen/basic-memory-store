from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, ClassVar, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


Role = Literal["user", "assistant", "system", "tool"]
RetrievalScope = Literal["conversation", "client", "owner"]
TimeWindow = Literal["7d", "30d", "90d", "all"]
RetrievalMode = Literal["recent", "balanced", "historical"]
RetrievalContractMode = Literal["augmented", "raw", "compare"]
RetrievalSourceType = Literal["message", "derived_text"]
ArtifactContentClass = Literal[
    "document",
    "code",
    "image",
    "screenshot",
    "audio",
    "video",
    "other",
]
RetrievalSensitivity = Literal["low", "medium", "high", "restricted"]
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
BoundedLabel = Annotated[str, Field(min_length=1, max_length=64)]
BoundedScopeId = Annotated[str, Field(min_length=1, max_length=160)]
RESERVED_POLICY_METADATA_KEY = "retrieval_policy_metadata"


def _normalize_label(value: str) -> str:
    cleaned = value.strip().lower().replace("-", "_").replace(" ", "_")
    return cleaned


def _normalize_labels(values: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = _normalize_label(value)
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        normalized.append(cleaned)
    return normalized


def _validate_bounded_string_list(
    value: Any,
    *,
    field_name: str,
    max_length: int,
    min_length: int = 0,
) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list")
    if len(value) < min_length:
        raise ValueError(f"{field_name} must contain at least {min_length} item")
    if len(value) > max_length:
        raise ValueError(f"{field_name} exceeds maximum length")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"{field_name} entries must be strings")
        cleaned = item.strip()
        if not cleaned:
            raise ValueError(f"{field_name} entries must be non-empty strings")
        out.append(cleaned)
    return out


class RetrievalRecordPolicyMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_domains: List[BoundedLabel] = Field(..., min_length=1, max_length=16)
    sensitivity: RetrievalSensitivity
    content_class: Optional[ArtifactContentClass] = None
    entity_ids: List[BoundedScopeId] = Field(default_factory=list, max_length=64)
    relationship_ids: List[BoundedScopeId] = Field(default_factory=list, max_length=64)
    relationship_scopes: List[BoundedLabel] = Field(default_factory=list, max_length=16)

    @field_validator("memory_domains", mode="before")
    @classmethod
    def validate_memory_domains_shape(cls, value: Any) -> list[str]:
        return _validate_bounded_string_list(value, field_name="memory_domains", min_length=1, max_length=16)

    @field_validator("entity_ids", "relationship_ids", mode="before")
    @classmethod
    def validate_scope_ids_shape(cls, value: Any) -> list[str]:
        return _validate_bounded_string_list(value, field_name="scope_ids", max_length=64)

    @field_validator("relationship_scopes", mode="before")
    @classmethod
    def validate_relationship_scopes_shape(cls, value: Any) -> list[str]:
        return _validate_bounded_string_list(value, field_name="relationship_scopes", max_length=16)

    @field_validator("memory_domains", "relationship_scopes", mode="after")
    @classmethod
    def normalize_label_lists(cls, values: list[str]) -> list[str]:
        return _normalize_labels(values)

    @field_validator("entity_ids", "relationship_ids", mode="after")
    @classmethod
    def dedupe_scope_ids(cls, values: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for value in values:
            cleaned = value.strip()
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            out.append(cleaned)
        return out


TEXTUAL_ARTIFACT_MIME_CLASSES = {
    "text/plain": "document",
    "text/markdown": "document",
    "application/json": "document",
}
SOURCE_CODE_EXTENSIONS = {
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".go",
    ".rs",
    ".java",
    ".c",
    ".cc",
    ".cpp",
    ".h",
    ".hpp",
    ".cs",
    ".rb",
    ".php",
    ".sh",
    ".sql",
    ".yaml",
    ".yml",
    ".toml",
}


def expected_artifact_content_classes_for_mime(mime: str, filename: str | None = None) -> set[str]:
    normalized = (mime or "").split(";")[0].strip().lower()
    suffix = ""
    if filename and "." in filename:
        suffix = "." + filename.rsplit(".", 1)[-1].lower()
    if normalized.startswith("image/"):
        return {"image", "screenshot"}
    if normalized.startswith("audio/"):
        return {"audio"}
    if normalized.startswith("video/"):
        return {"video"}
    if suffix in SOURCE_CODE_EXTENSIONS:
        return {"code"}
    if normalized in TEXTUAL_ARTIFACT_MIME_CLASSES:
        return {"document", "code"} if normalized == "text/plain" else {TEXTUAL_ARTIFACT_MIME_CLASSES[normalized]}
    return {"other"}


def validate_artifact_policy_metadata_for_mime(
    policy_metadata: RetrievalRecordPolicyMetadata | None,
    *,
    mime: str,
    filename: str | None = None,
) -> RetrievalRecordPolicyMetadata | None:
    normalized = (mime or "").split(";")[0].strip().lower()
    if policy_metadata is None:
        if normalized.startswith(("image/", "audio/", "video/")):
            raise ValueError("media artifact policy metadata requires content_class")
        return None
    if policy_metadata.sensitivity == "restricted":
        raise ValueError("restricted artifact policy metadata is not retrievable")
    if policy_metadata.content_class is None:
        raise ValueError("artifact policy metadata requires content_class")
    allowed = expected_artifact_content_classes_for_mime(mime, filename=filename)
    if policy_metadata.content_class not in allowed:
        raise ValueError("artifact content_class contradicts MIME type")
    return policy_metadata


class ArtifactAccessPolicyInput(BaseModel):
    enforcement_mode: Literal["mandatory"] = "mandatory"
    allowed_content_classes: List[ArtifactContentClass] = Field(..., max_length=8)
    allowed_domains: List[BoundedLabel] = Field(..., max_length=16)
    maximum_sensitivity: RetrievalSensitivity
    surface_content_capabilities: List[ArtifactContentClass] = Field(..., max_length=8)
    reason_codes: List[BoundedLabel] = Field(..., max_length=8)

    @field_validator("allowed_domains", "reason_codes", mode="after")
    @classmethod
    def normalize_labels(cls, values: list[str]) -> list[str]:
        return _normalize_labels(values)


class RelationshipRetrievalScopeProjectionInput(BaseModel):
    applied: bool = False
    relationship_ids: List[BoundedScopeId] = Field(default_factory=list, max_length=64)
    entity_ids: List[BoundedScopeId] = Field(default_factory=list, max_length=64)
    relationship_scopes: List[BoundedLabel] = Field(default_factory=list, max_length=16)
    reason_codes: List[BoundedLabel] = Field(default_factory=list, max_length=8)

    @field_validator("relationship_scopes", "reason_codes", mode="after")
    @classmethod
    def normalize_labels(cls, values: list[str]) -> list[str]:
        return _normalize_labels(values)

    @field_validator("relationship_ids", "entity_ids", mode="after")
    @classmethod
    def dedupe_scope_ids(cls, values: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for value in values:
            cleaned = value.strip()
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            out.append(cleaned)
        return out

    @model_validator(mode="after")
    def validate_applied_scope(self):
        if self.applied and not self.relationship_ids and not self.entity_ids:
            raise ValueError("applied relationship scope requires a relationship_id or entity_id")
        return self


class RetrievalContainmentPolicy(BaseModel):
    enforcement_mode: Literal["mandatory"]
    allowed_memory_domains: List[BoundedLabel] = Field(..., max_length=16)
    blocked_memory_domains: List[BoundedLabel] = Field(default_factory=list, max_length=16)
    artifact_access_policy: ArtifactAccessPolicyInput
    relationship_scope_projection: Optional[RelationshipRetrievalScopeProjectionInput] = None

    @field_validator("allowed_memory_domains", "blocked_memory_domains", mode="after")
    @classmethod
    def normalize_domains(cls, values: list[str]) -> list[str]:
        return _normalize_labels(values)


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
    RESERVED_POLICY_METADATA_KEY: ClassVar[str] = RESERVED_POLICY_METADATA_KEY

    owner_id: str = Field(..., examples=["owner_123"])
    role: Role = Field(..., examples=["user"])
    content: str = Field(..., examples=["Hello world"])
    client_id: Optional[str] = Field(default=None, examples=["phone"])
    metadata: Optional[Dict[str, Any]] = Field(default=None, description="Arbitrary JSON metadata.")
    policy_metadata: Optional[RetrievalRecordPolicyMetadata] = None

    @model_validator(mode="after")
    def reject_reserved_metadata_key(self):
        if isinstance(self.metadata, dict) and self.RESERVED_POLICY_METADATA_KEY in self.metadata:
            raise ValueError("metadata cannot contain reserved retrieval policy metadata")
        return self


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
    policy_metadata: Optional[RetrievalRecordPolicyMetadata] = None

    @model_validator(mode="after")
    def validate_content_class_matches_mime(self):
        validate_artifact_policy_metadata_for_mime(self.policy_metadata, mime=self.mime, filename=self.filename)
        return self


class ArtifactInitResponse(BaseModel):
    artifact_id: str
    upload_url: str
    upload_url_expires_in_s: int
    object_uri: str
    status: str


class ArtifactCompleteRequest(BaseModel):
    artifact_id: str
    owner_id: Optional[str] = None
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
    policy_metadata: Optional[RetrievalRecordPolicyMetadata] = None
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
    containment_policy: Optional[RetrievalContainmentPolicy] = None


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
    containment_policy: Optional[RetrievalContainmentPolicy] = None

    @model_validator(mode="after")
    def validate_legacy_domain_compatibility(self):
        if self.containment_policy is None:
            return self
        if self.allowed_memory_domains is not None:
            legacy_allowed = set(_normalize_labels(self.allowed_memory_domains))
            mandatory_allowed = set(self.containment_policy.allowed_memory_domains)
            if legacy_allowed != mandatory_allowed:
                raise ValueError("legacy allowed_memory_domains must match containment policy")
        if self.blocked_memory_domains is not None:
            legacy_blocked = set(_normalize_labels(self.blocked_memory_domains))
            mandatory_blocked = set(self.containment_policy.blocked_memory_domains)
            if legacy_blocked != mandatory_blocked:
                raise ValueError("legacy blocked_memory_domains must match containment policy")
        return self


class RetrievalSourceRef(BaseModel):
    ref_type: RetrievalSourceType
    ref_id: str


class RetrievalPolicyMetadata(BaseModel):
    memory_domains: List[str] = Field(default_factory=list)
    sensitivity: Optional[str] = None
    content_class: Optional[str] = None
    entity_ids: List[str] = Field(default_factory=list)
    relationship_ids: List[str] = Field(default_factory=list)
    relationship_scopes: List[str] = Field(default_factory=list)


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
    policy_metadata: Optional[RetrievalRecordPolicyMetadata] = None


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
    RESERVED_POLICY_METADATA_KEY: ClassVar[str] = RESERVED_POLICY_METADATA_KEY

    request_id: str
    owner_id: str
    source_type: str
    source_event_id: str
    event_type: str
    event_time: Optional[datetime] = None
    payload_json: Dict[str, Any] = Field(default_factory=dict)
    entities: List[EventEntityIn] = Field(default_factory=list)
    policy_metadata: Optional[RetrievalRecordPolicyMetadata] = None

    @model_validator(mode="after")
    def reject_payload_spoofing(self):
        if self.RESERVED_POLICY_METADATA_KEY in self.payload_json:
            raise ValueError("payload_json cannot contain reserved retrieval policy metadata")
        return self


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
MemoryPromotionDecision = Literal["promote", "update", "suppress", "defer"]
MemoryPromotionTargetType = Literal["short_horizon", "core", "procedural", "episodic", "dormant"]


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


class MemoryEvaluateRequest(BaseModel):
    request_id: str = Field(..., min_length=1, max_length=160)
    owner_id: str = Field(..., min_length=1, max_length=160)
    candidate: Dict[str, Any] = Field(..., min_length=1)
    persist_decision: bool = False


class MemoryEvaluateResponse(BaseModel):
    request_id: str
    owner_id: str
    decision: MemoryPromotionDecision
    target_memory_type: MemoryPromotionTargetType
    factor_scores: Dict[str, float] = Field(default_factory=dict)
    promotion_score: float
    suppression_reasons: List[str] = Field(default_factory=list)
    defer_reasons: List[str] = Field(default_factory=list)
    reasons: Dict[str, Any] = Field(default_factory=dict)
    decision_record: Optional[Dict[str, Any]] = None


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


class MemoryDecayRequest(BaseModel):
    request_id: str = Field(..., min_length=1, max_length=160)
    owner_id: str = Field(..., min_length=1, max_length=160)
    reason: MemoryTransitionReason
    decay_factor: float = Field(default=0.25, ge=0.0, le=1.0)
    demote: bool = False


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
EpisodeExtractionDecisionValue = Literal["accept", "reject", "defer"]
EpisodeCallbackDecisionValue = Literal["include", "suppress", "defer"]
EpisodeCallbackStrategy = Literal["none", "light_callback", "explicit_callback"]


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


class EpisodeSourceItem(BaseModel):
    id: Optional[str] = Field(default=None, max_length=160)
    message_id: Optional[str] = Field(default=None, max_length=160)
    event_id: Optional[str] = Field(default=None, max_length=160)
    owner_id: Optional[str] = Field(default=None, max_length=160)
    role: Optional[str] = Field(default=None, max_length=64)
    content: Optional[str] = Field(default=None, max_length=4000)
    text: Optional[str] = Field(default=None, max_length=4000)
    summary: Optional[str] = Field(default=None, max_length=4000)
    title: Optional[str] = Field(default=None, max_length=400)
    description: Optional[str] = Field(default=None, max_length=4000)
    event_text: Optional[str] = Field(default=None, max_length=4000)
    source_ref: Optional[EpisodeSourceRef] = None
    outcome: Optional[str] = Field(default=None, max_length=1000)
    unresolved: Dict[str, Any] = Field(default_factory=dict)
    time_window: Dict[str, Any] = Field(default_factory=dict)
    participants: List[Any] = Field(default_factory=list, max_length=20)
    entities: List[Any] = Field(default_factory=list, max_length=20)
    unsupported: bool = False
    evidence_supported: Optional[bool] = None


class EpisodeExtractRequest(BaseModel):
    request_id: str = Field(..., min_length=1, max_length=160)
    owner_id: str = Field(..., min_length=1, max_length=160)
    conversation_id: Optional[str] = Field(default=None, max_length=160)
    scene: Dict[str, Any] = Field(default_factory=dict)
    source_items: List[EpisodeSourceItem] = Field(..., min_length=1, max_length=50)
    persist: bool = True


class EpisodeExtractionDecisionItem(BaseModel):
    decision_id: str
    decision: EpisodeExtractionDecisionValue
    episode_type: str
    reasons: List[str] = Field(default_factory=list)
    episode_key: Optional[str] = None
    title: Optional[str] = None
    summary: Optional[str] = None
    trigger: Dict[str, Any] = Field(default_factory=dict)
    outcome: Optional[str] = None
    significance: Optional[str] = None
    unresolved: Dict[str, Any] = Field(default_factory=dict)
    evidence: List[Dict[str, Any]] = Field(default_factory=list)
    source_refs: List[Dict[str, Any]] = Field(default_factory=list)
    time_window: Dict[str, Any] = Field(default_factory=dict)
    participants: List[Any] = Field(default_factory=list)
    entities: List[Any] = Field(default_factory=list)
    callback_candidates: List[Any] = Field(default_factory=list)
    confidence: Optional[float] = None
    episode: Optional[EpisodeItemResponse] = None
    created: bool = False
    updated: bool = False


class EpisodeExtractResponse(BaseModel):
    request_id: str
    owner_id: str
    accepted_count: int
    rejected_count: int
    deferred_count: int
    decisions: List[EpisodeExtractionDecisionItem] = Field(default_factory=list)


class EpisodeCallbackContext(BaseModel):
    scene_id: Optional[str] = Field(default=None, max_length=160)
    surface: Optional[str] = Field(default=None, max_length=80)
    urgency: Optional[str] = Field(default=None, max_length=80)
    sensitivity: Optional[str] = Field(default=None, max_length=80)


class EpisodeCallbackCandidate(BaseModel):
    episode_id: Optional[str] = Field(default=None, max_length=160)
    candidate_id: Optional[str] = Field(default=None, max_length=160)
    episode_key: Optional[str] = Field(default=None, max_length=160)
    title: Optional[str] = Field(default=None, max_length=400)
    summary: Optional[str] = Field(default=None, max_length=4000)
    episode_type: Optional[str] = Field(default=None, max_length=80)
    source_refs: List[EpisodeSourceRef] = Field(default_factory=list, max_length=50)
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    significance: Optional[str] = Field(default=None, max_length=4000)
    unresolved: Dict[str, Any] = Field(default_factory=dict)
    time_window: Dict[str, Any] = Field(default_factory=dict)
    scene: Dict[str, Any] = Field(default_factory=dict)
    relevance_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    continuity_value: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    recency_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    awkwardness_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class EpisodeCallbackEvaluateRequest(BaseModel):
    request_id: str = Field(..., min_length=1, max_length=160)
    owner_id: str = Field(..., min_length=1, max_length=160)
    context: EpisodeCallbackContext = Field(default_factory=EpisodeCallbackContext)
    candidates: List[EpisodeCallbackCandidate] = Field(..., min_length=1, max_length=100)


class EpisodeCallbackDecisionItem(BaseModel):
    episode_id: str
    decision: EpisodeCallbackDecisionValue
    callback_strategy: EpisodeCallbackStrategy
    callback_score: float
    prompt_eligible: bool
    reasons: List[str] = Field(default_factory=list)
    signals: Dict[str, Any] = Field(default_factory=dict)
    episode: Dict[str, Any] = Field(default_factory=dict)


class EpisodeCallbackEvaluateResponse(BaseModel):
    request_id: str
    owner_id: str
    decision_count: int
    decisions: List[EpisodeCallbackDecisionItem] = Field(default_factory=list)


class EpisodeRetrieveRequest(BaseModel):
    request_id: str = Field(..., min_length=1, max_length=160)
    owner_id: str = Field(..., min_length=1, max_length=160)
    context: EpisodeCallbackContext = Field(default_factory=EpisodeCallbackContext)
    limit: int = Field(default=10, ge=1, le=50)


class EpisodeRetrieveResponse(BaseModel):
    request_id: str
    owner_id: str
    candidate_count: int
    eligible_count: int
    decisions: List[EpisodeCallbackDecisionItem] = Field(default_factory=list)


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


# ---- Claim records ----

ClaimRecordIdentifier = Annotated[
    str,
    Field(
        min_length=1,
        max_length=120,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]
ClaimEvidenceRefType = Literal[
    "message",
    "derived_text",
    "artifact",
    "external_source",
    "world_state_claim",
    "tool_output",
    "integration_event",
]
ClaimEvidenceSupportKind = Literal[
    "direct",
    "corroborating",
    "contextual",
    "contradictory",
]
ClaimEvidenceAuthority = Literal[
    "peer_reviewed_evidence",
    "clinical_guidance",
    "manufacturer_guidance",
    "tool_output",
    "trusted_integration",
    "user_report",
    "runtime_inference",
    "speculation",
    "unknown",
]
ClaimEvidenceFreshnessState = Literal[
    "active",
    "stale",
    "superseded",
    "corrected",
    "unknown_freshness",
    "not_applicable",
]
ClaimClass = Literal[
    "verified_fact",
    "source_backed_fact",
    "manufacturer_guidance",
    "expert_consensus",
    "runtime_inference",
    "speculation",
    "unknown",
]
ClaimCalibrationStatus = Literal["supported", "limited", "unsupported"]
ClaimEvidenceStrength = Literal["strong", "moderate", "weak", "none"]
ClaimConfidence = Literal["high", "medium", "low", "unknown"]
ClaimFreshnessSummary = Literal["current", "mixed", "stale", "unknown", "not_applicable"]
ClaimLimitationCode = Literal[
    "no_supporting_evidence",
    "context_only",
    "low_authority_evidence",
    "stale_evidence",
    "unknown_freshness",
    "superseded_or_corrected_evidence",
    "contradictory_evidence",
    "single_source",
    "inference_dominant",
    "speculation_only",
]


class ClaimEvidenceReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref_type: ClaimEvidenceRefType
    ref_id: ClaimRecordIdentifier
    owner_id: ClaimRecordIdentifier
    conversation_id: ClaimRecordIdentifier | None = None
    support_kind: ClaimEvidenceSupportKind
    authority: ClaimEvidenceAuthority
    freshness_state: ClaimEvidenceFreshnessState


class ClaimRecordCalibrationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_id: ClaimRecordIdentifier
    claim_anchor: Annotated[str, Field(min_length=1, max_length=500)]
    claim_anchor_digest: Annotated[
        str,
        Field(pattern=r"^sha256:[0-9a-f]{64}$"),
    ]
    claim_class: ClaimClass
    calibration_status: ClaimCalibrationStatus
    evidence_strength: ClaimEvidenceStrength
    confidence: ClaimConfidence
    strongest_authority: ClaimEvidenceAuthority
    freshness_summary: ClaimFreshnessSummary
    uncertainty_disclosure_required: bool
    validated_evidence_references: List[ClaimEvidenceReference] = Field(
        default_factory=list,
        max_length=16,
    )
    limitation_codes: List[ClaimLimitationCode] = Field(default_factory=list, max_length=10)
    user_safe_summary: Annotated[str, Field(min_length=1, max_length=500)]

    @field_validator("claim_anchor", mode="before")
    @classmethod
    def normalize_claim_anchor(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        return " ".join(value.split())

    @model_validator(mode="after")
    def validate_bounded_collections(self):
        identities = [
            (reference.ref_type, reference.ref_id)
            for reference in self.validated_evidence_references
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate_evidence_reference")
        if len(self.limitation_codes) != len(set(self.limitation_codes)):
            raise ValueError("duplicate_limitation_code")
        self.validated_evidence_references = sorted(
            self.validated_evidence_references,
            key=lambda item: (
                item.ref_type,
                item.ref_id,
                item.owner_id,
                item.conversation_id or "",
                item.support_kind,
                item.authority,
                item.freshness_state,
            ),
        )
        return self


class ClaimRecordCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["claim-record.v1"]
    request_id: ClaimRecordIdentifier
    owner_id: ClaimRecordIdentifier
    conversation_id: ClaimRecordIdentifier
    assistant_message_id: ClaimRecordIdentifier
    surface: Annotated[str, Field(min_length=1, max_length=64)]
    runtime_session_id: ClaimRecordIdentifier
    runtime_turn_id: ClaimRecordIdentifier
    calibration_result: ClaimRecordCalibrationResult

    @model_validator(mode="after")
    def validate_evidence_scope(self):
        for reference in self.calibration_result.validated_evidence_references:
            if reference.owner_id != self.owner_id:
                raise ValueError("evidence_owner_mismatch")
            if (
                reference.conversation_id is not None
                and reference.conversation_id != self.conversation_id
            ):
                raise ValueError("evidence_conversation_mismatch")
        return self


class ClaimRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_id: ClaimRecordIdentifier
    schema_version: Literal["claim-record.v1"]
    owner_id: ClaimRecordIdentifier
    conversation_id: ClaimRecordIdentifier
    request_id: ClaimRecordIdentifier
    assistant_message_id: ClaimRecordIdentifier
    surface: Annotated[str, Field(min_length=1, max_length=64)]
    runtime_session_id: ClaimRecordIdentifier
    runtime_turn_id: ClaimRecordIdentifier
    claim_anchor: Annotated[str, Field(min_length=1, max_length=500)]
    claim_anchor_digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    claim_class: ClaimClass
    calibration_status: ClaimCalibrationStatus
    evidence_strength: ClaimEvidenceStrength
    confidence: ClaimConfidence
    strongest_authority: ClaimEvidenceAuthority
    freshness_summary: ClaimFreshnessSummary
    uncertainty_disclosure_required: bool
    validated_evidence_references: List[ClaimEvidenceReference] = Field(max_length=16)
    limitation_codes: List[ClaimLimitationCode] = Field(max_length=10)
    user_safe_summary: Annotated[str, Field(min_length=1, max_length=500)]
    created_at: str


class ClaimRecordCreateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    created: bool
    record: ClaimRecord


class ClaimRecordListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    records: List[ClaimRecord]


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
