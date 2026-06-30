from __future__ import annotations

import logging
import math
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, UTC, timedelta
from typing import Any
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException, Security, Request, Response
from fastapi.security.api_key import APIKeyHeader
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest

from settings import get_settings
from clients.litellm import LiteLLMClient
from storage.postgres import PostgresStore
from storage.qdrant import QdrantStore, RetrievalHit as QdrantHit
from storage.object_store import ObjectStoreClient
from prompts.context import assemble_messages, build_artifact_context_block, build_context_block
from services.ingestion import (
    TEXT_ARTIFACT_DERIVATION_VERSION,
    derive_text_chunks_for_artifact,
    ingest_files,
    is_supported_text_artifact_mime,
)
from services.retrieval import build_retrieval_response_payload, doctrine_diagnostics_for_bundle
from services.proactive import evaluate_event as evaluate_initiative_event
from services.memory_items import normalize_scores, normalize_source_refs, shape_memory_event, shape_memory_item, source_ref_hash
from services.episodes import DEFAULT_DERIVATION_VERSION as EPISODE_DERIVATION_VERSION, episode_key, normalize_json_list, normalize_json_map, normalize_source_refs as normalize_episode_source_refs, shape_episode, shape_episode_event, shape_episode_link, source_ref_hash as episode_source_ref_hash
from services.recall import select_recall_decision, shape_recall_decision
from services.derived_contract import CONTRACT_ADAPTERS
from services.derivation_lifecycle import (
    DERIVED_CLASSES,
    inspect_row as inspect_lifecycle_row,
    invalidate_derived,
    load_derived_row,
    replay_derived,
    structural_hash,
)

from models import (
    ArtifactCompleteRequest,
    ArtifactInitRequest,
    ArtifactInitResponse,
    ArtifactResponse,
    ChatRequest,
    ChatResponse,
    ConversationCreateRequest,
    ConversationCreateResponse,
    ConversationListResponse,
    ConversationSummary,
    OrchestrateChatRequest,
    OrchestrateChatResponse,
    ConversationResolveRequest,
    ConversationResolveResponse,
    EventIngestRequest,
    EventIngestResponse,
    InitiativeDebugResponse,
    InitiativeDetailResponse,
    InitiativeEvaluateRequest,
    InitiativeEvaluateResponse,
    InitiativeFeedbackRequest,
    InitiativeFeedbackResponse,
    ProactiveDeliveryAttemptRequest,
    ProactiveDeliveryAttemptResponse,
    ProactiveEvaluateRequest,
    ProactiveEvaluateResponse,
    ProactivePrefsResponse,
    ProactivePrefsUpdateRequest,
    ProactiveSuggestionFeedbackRequest,
    ProactiveSuggestionFeedbackResponse,
    ProactiveSuggestionItem,
    ProactiveSuggestionListResponse,
    MessageCreateRequest,
    MessageCreateResponse,
    EpisodeCreateRequest,
    EpisodeCreateResponse,
    EpisodeDebugResponse,
    EpisodeEventItem,
    EpisodeItemResponse,
    EpisodeLinkItem,
    EpisodeLinkRequest,
    EpisodeLinkResponse,
    DerivedInspectionResponse,
    DerivedInvalidationRequest,
    DerivedInvalidationResponse,
    DerivedLifecycleInspection,
    DerivedReplayRequest,
    DerivedReplayResponse,
    MemoryDebugResponse,
    MemoryEventItem,
    MemoryItemResponse,
    MemoryPromoteRequest,
    MemoryPromoteResponse,
    MemoryReinforceRequest,
    MemoryTransitionRequest,
    MemoryTransitionResponse,
    RecallDebugResponse,
    RecallDecisionItem,
    RecallSelectRequest,
    RecallSelectResponse,
    RetrieveRequest,
    RetrieveResponse,
    RetrieveHit,
    RetrievalOptions,
    TieredRetrieveRequest,
    TieredRetrieveResponse,
    OverlayItem,
    RetrievalDebug,
    RetrievalDebugHit,
    RetrieveBundleRequest,
    RetrieveBundleResponse,
    ArtifactRef,
    FileIngestionRequest,
    FileIngestionResponse,
    HygieneFlagItem,
    HygieneFlagListResponse,
    HygieneScanRequest,
    HygieneScanResponse,
    ProfileResolveRequest,
    ProfileResolveResponse,
    SurfaceContext,
    TraceCreateRequest,
    TraceCreateResponse,
    TraceResponse,
)


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

settings = get_settings()

# --- Auth: adds Swagger "Authorize" for X-API-Key ---
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def require_api_key(api_key: str | None = Security(api_key_header)) -> None:
    if not api_key or api_key != settings.memory_api_key:
        raise HTTPException(status_code=401, detail="Unauthorized")


# Apply auth globally to avoid forgetting it per-route.
# (If you want /healthz and /readyz to be public later, we can split routers.)
@asynccontextmanager
async def lifespan(app: FastAPI):
    await pg.open()
    if getattr(settings, "object_store_enabled", False):
        object_store.ensure_bucket()
    try:
        yield
    finally:
        await pg.close()


app = FastAPI(
    title="Basic Memory Store",
    version="0.1.0",
    lifespan=lifespan,
    swagger_ui_parameters={
        "persistAuthorization": True,
        "displayRequestDuration": True,
    },
)


@app.middleware("http")
async def attach_request_id(request: Request, call_next):
    """Echo X-Request-ID only when provided by caller."""
    rid = request.headers.get("X-Request-ID")
    request.state.request_id = rid
    response = await call_next(request)
    if rid:
        response.headers["X-Request-ID"] = rid
    return response



# --- Core clients/stores ---
pg = PostgresStore(settings.pg_dsn)
litellm = LiteLLMClient(settings.litellm_base_url, settings.litellm_api_key)
qdrant = QdrantStore(settings.qdrant_url, settings.qdrant_collection, litellm, settings.embed_model)
object_store = ObjectStoreClient(
    endpoint_url=settings.object_store_endpoint,
    bucket=settings.object_store_bucket,
    access_key=settings.object_store_access_key,
    secret_key=settings.object_store_secret_key,
    region=settings.object_store_region,
    presign_base_url=settings.object_store_presign_base_url,
    include_content_type_in_put_signature=settings.object_store_include_content_type_in_put_signature,
)
memory_skipped_qdrant_ids_total = Counter(
    "memory_skipped_qdrant_ids_total",
    "Count of non-UUID Qdrant hit ids skipped by the API",
    ["kind"],
)


def should_index_message(role: str, content: str) -> bool:
    """Heuristic indexing policy to reduce retrieval noise."""
    if not content or not content.strip():
        return False

    if len(content.strip()) < settings.min_index_chars:
        return False

    if role == "assistant" and not settings.index_assistant_messages:
        return False

    if role == "user" and (not settings.index_user_questions) and content.strip().endswith("?"):
        return False

    return True


def _require_matching_request_id(request: Request, body_request_id: str) -> str:
    header_request_id = request.headers.get("X-Request-ID")
    if settings.require_request_id and not header_request_id:
        raise HTTPException(status_code=400, detail="X-Request-ID header is required")
    if not body_request_id:
        raise HTTPException(status_code=400, detail="request_id is required in request body")
    if settings.enforce_request_id_header_body_match and header_request_id != body_request_id:
        raise HTTPException(status_code=400, detail="request_id must match X-Request-ID")
    return body_request_id


def _sanitize_object_key_component(name: str) -> str:
    cleaned = name.strip()
    cleaned = cleaned.replace("\\", "_").replace("/", "_")
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"[^A-Za-z0-9._ ()-]", "_", cleaned)
    cleaned = cleaned.strip()
    return cleaned or "artifact"


def _safe_uuid_message_ids(hits: list[QdrantHit], *, context: str, kind: str) -> list[UUID]:
    out: list[UUID] = []
    for h in hits:
        try:
            out.append(UUID(h.message_id))
        except (TypeError, ValueError):
            memory_skipped_qdrant_ids_total.labels(kind=kind).inc()
            logging.warning("Skipping non-UUID retrieval hit id in %s: %r", context, getattr(h, "message_id", None))
    return out


def _safe_uuid_ids(raw_ids: list[str], *, context: str, kind: str) -> list[UUID]:
    out: list[UUID] = []
    for item in raw_ids:
        try:
            out.append(UUID(item))
        except (TypeError, ValueError):
            memory_skipped_qdrant_ids_total.labels(kind=kind).inc()
            logging.warning("Skipping non-UUID retrieval hit id in %s: %r", context, item)
    return out


def _cap_snippet(text: str, max_chars: int) -> str:
    cleaned = text.strip()
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max_chars - 12].rstrip() + "...(trunc)"


def _retrieval_artifact_k() -> int:
    return int(getattr(settings, "retrieval_artifact_k", 3))


def _retrieval_artifact_max_snippet_chars() -> int:
    return int(getattr(settings, "retrieval_artifact_max_snippet_chars", 500))


def _normalize_surface_type(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in {"", "unknown"}:
        return None
    return normalized


def _resolve_surface_behavior(surface: str | None, surface_context: SurfaceContext | None) -> dict[str, Any]:
    legacy_surface = _normalize_surface_type(surface)
    nested_surface = _normalize_surface_type(surface_context.surface_type) if surface_context else None
    resolved_surface = nested_surface or legacy_surface or "chat"

    interaction_mode = surface_context.interaction_mode if surface_context and surface_context.interaction_mode else None
    spoken_output = surface_context.spoken_output if surface_context and surface_context.spoken_output is not None else None
    active_task_mode = bool(surface_context.active_task_mode) if surface_context and surface_context.active_task_mode is not None else False
    latency_preference = surface_context.latency_preference if surface_context and surface_context.latency_preference else "normal"
    verbosity_target = surface_context.verbosity_target if surface_context and surface_context.verbosity_target else None
    allows_expansion = surface_context.allows_expansion if surface_context and surface_context.allows_expansion is not None else True
    output_format = surface_context.output_format if surface_context and surface_context.output_format else None

    if interaction_mode is None:
        interaction_mode = "voice_mediated" if resolved_surface in {"voice", "car", "alexa"} else "text"
    if spoken_output is None:
        spoken_output = interaction_mode == "voice_mediated" or resolved_surface in {"voice", "car", "alexa"}
    if output_format is None:
        output_format = "speech" if spoken_output else "plain_text"
    if verbosity_target is None:
        verbosity_target = "short" if active_task_mode else "normal"

    compatibility_note = None
    if nested_surface and legacy_surface and nested_surface != legacy_surface:
        compatibility_note = {
            "kind": "surface_type_override",
            "top_level_surface": legacy_surface,
            "surface_context_surface_type": nested_surface,
            "resolved_surface_type": resolved_surface,
        }

    return {
        "surface_type": resolved_surface,
        "interaction_mode": interaction_mode,
        "spoken_output": spoken_output,
        "active_task_mode": active_task_mode,
        "latency_preference": latency_preference,
        "verbosity_target": verbosity_target,
        "allows_expansion": allows_expansion,
        "output_format": output_format,
        "style_envelope": surface_context.style_envelope if surface_context else {},
        "compatibility_note": compatibility_note,
    }


def _time_window_cutoff(time_window: str) -> datetime | None:
    now = datetime.now(UTC)
    if time_window == "7d":
        return now - timedelta(days=7)
    if time_window == "30d":
        return now - timedelta(days=30)
    if time_window == "90d":
        return now - timedelta(days=90)
    return None


def _half_life_days(retrieval_mode: str) -> int:
    if retrieval_mode == "recent":
        return int(settings.retrieval_recent_half_life_days)
    if retrieval_mode == "historical":
        return int(settings.retrieval_historical_half_life_days)
    return int(settings.retrieval_balanced_half_life_days)


def _safe_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace(" ", "T"))
    except ValueError:
        return None


def _normalize_source_type(source_type: str) -> tuple[str, str | None, str]:
    normalized = source_type.strip().lower()
    if normalized not in {"git", "calendar", "finance", "portfolio"}:
        raise HTTPException(status_code=400, detail="source_type must be one of: git, calendar, finance, portfolio")

    routed = "portfolio" if normalized == "finance" else normalized
    stream_key = f"event-stream:{routed}"
    original = normalized if normalized != routed else None
    return routed, original, stream_key


def _event_stream_title(source_type: str) -> str:
    return f"{source_type} event stream"


def _render_event_message_content(
    *,
    source_type: str,
    event_type: str,
    source_event_id: str,
    event_time: datetime | None,
    payload_json: dict[str, Any],
) -> str:
    lines = [
        f"{source_type.capitalize()} event: {event_type.replace('_', ' ')}.",
        f"Source event id: {source_event_id}.",
    ]
    if event_time is not None:
        lines.append(f"Event time: {event_time.isoformat()}.")
    summary = payload_json.get("summary")
    if isinstance(summary, str) and summary.strip():
        lines.append(f"Summary: {summary.strip()}")
    title = payload_json.get("title")
    if isinstance(title, str) and title.strip():
        lines.append(f"Title: {title.strip()}")
    for key in ("repo", "branch", "location", "account", "symbol"):
        value = payload_json.get(key)
        if isinstance(value, str) and value.strip():
            lines.append(f"{key.capitalize()}: {value.strip()}")
    return "\n".join(lines)


def _message_missing_score(item: dict[str, object]) -> float:
    metadata = item.get("metadata") if isinstance(item, dict) else None
    if not isinstance(metadata, dict):
        return 0.0
    score = 0.0
    if metadata.get("artifact_expected") and not metadata.get("artifact_ids"):
        score += 0.08
    if metadata.get("dangling_reference"):
        score += 0.05
    return min(score, float(settings.retrieval_missing_penalty_cap))


def _artifact_missing_score(item: dict[str, object]) -> float:
    derivation_params = item.get("derivation_params") if isinstance(item, dict) else None
    if not isinstance(derivation_params, dict):
        return 0.0
    score = 0.0
    if not item.get("file_path"):
        score += 0.08
    if derivation_params.get("linked_entities_missing"):
        score += 0.05
    return min(score, float(settings.retrieval_missing_penalty_cap))


def _score_item(
    *,
    semantic_score: float | None,
    created_at: str | None,
    retrieval_mode: str,
    is_same_conversation: bool,
    is_pinned: bool,
    missing_score: float,
) -> dict[str, float]:
    base_score = float(semantic_score or 0.0)
    recency_adjustment = 0.0
    created_dt = _safe_dt(created_at)
    if created_dt is not None:
        age_days = max(0.0, (datetime.now(UTC) - created_dt).total_seconds() / 86400.0)
        boost = math.exp(-(age_days / max(1, _half_life_days(retrieval_mode))))
        if retrieval_mode == "recent":
            recency_adjustment = 0.2 * boost
        elif retrieval_mode == "historical":
            recency_adjustment = 0.05 * boost
        else:
            recency_adjustment = 0.12 * boost

    conversation_boost = float(settings.retrieval_conversation_boost) if is_same_conversation else 0.0
    # The current v2 retrieval bundle does not include pinned memories.
    # Pinned memories remain exposed separately through the unchanged tiered retrieval path.
    pinned_bias = float(settings.retrieval_pinned_bias) if is_pinned else 0.0
    final_score = base_score + recency_adjustment + conversation_boost + pinned_bias - missing_score
    return {
        "semantic_score": round(base_score, 6),
        "recency_adjustment": round(recency_adjustment, 6),
        "conversation_boost": round(conversation_boost, 6),
        "pinned_bias": round(pinned_bias, 6),
        "missing_score": round(missing_score, 6),
        "final_score": round(final_score, 6),
    }


def _in_time_window(created_at: str | None, time_window: str) -> bool:
    cutoff = _time_window_cutoff(time_window)
    if cutoff is None:
        return True
    created_dt = _safe_dt(created_at)
    if created_dt is None:
        return True
    return created_dt >= cutoff


def _dedupe_artifact_refs(refs: list[ArtifactRef]) -> list[ArtifactRef]:
    best_by_key: dict[tuple[str | None, str, str], ArtifactRef] = {}
    order: list[tuple[str | None, str, str]] = []

    for ref in refs:
        key = (ref.repo_name, ref.file_path, ref.snippet)
        existing = best_by_key.get(key)
        if existing is None:
            best_by_key[key] = ref
            order.append(key)
            continue

        existing_score = existing.relevance_score if existing.relevance_score is not None else float("-inf")
        candidate_score = ref.relevance_score if ref.relevance_score is not None else float("-inf")
        if candidate_score > existing_score:
            best_by_key[key] = ref

    return [best_by_key[key] for key in order]


def _normalize_hygiene_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def build_artifact_object_uri(owner_id: str, artifact_id: UUID, filename: str) -> str:
    safe_owner = _sanitize_object_key_component(owner_id)
    safe_name = _sanitize_object_key_component(filename)
    ts = datetime.now(UTC)
    return f"{settings.artifacts_object_prefix.rstrip('/')}/{safe_owner}/{ts:%Y/%m}/{artifact_id}/{safe_name}"


def build_artifact_transfer_url(kind: str, artifact_id: str) -> str:
    return f"{settings.artifacts_upload_base_url.rstrip('/')}/{kind}/{artifact_id}"


@app.get("/healthz", tags=["ops"], summary="Liveness probe")
async def healthz():
    dependencies = {"postgres": "unknown", "qdrant": "unknown"}

    try:
        await pg.ping()
        dependencies["postgres"] = "ok"
    except Exception as e:  # best effort only
        dependencies["postgres"] = f"error:{type(e).__name__}"

    try:
        qdrant.ping()
        dependencies["qdrant"] = "ok"
    except Exception as e:  # best effort only
        dependencies["qdrant"] = f"error:{type(e).__name__}"

    return {
        "ok": True,
        "status": "ok",
        "service": "basic-memory-store",
        "time": datetime.now(UTC).isoformat(),
        "dependencies": dependencies,
    }


@app.get("/readyz", tags=["ops"], summary="Readiness probe")
async def readyz():
    try:
        await pg.ping()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"postgres not ready: {e}")

    try:
        qdrant.ping()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"qdrant not ready: {e}")

    return {"ok": True}


@app.get("/metrics", tags=["ops"], summary="Prometheus metrics")
async def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)



# -------------------------
# Conversations
# -------------------------

@app.post(
    "/v1/conversations",
    response_model=ConversationCreateResponse,
    tags=["conversations"],
    dependencies=[Depends(require_api_key)],
    summary="Create a new conversation",
)
async def create_conversation(body: ConversationCreateRequest):
    cid = await pg.create_conversation(owner_id=body.owner_id, client_id=body.client_id, title=body.title)
    return ConversationCreateResponse(conversation_id=str(cid))

@app.get(
    "/v1/conversations",
    response_model=ConversationListResponse,
    tags=["conversations"],
    dependencies=[Depends(require_api_key)],
    summary="List conversations (most recent first)",
)
async def list_conversations(owner_id: str, client_id: str | None = None, limit: int = 20, cursor: str | None = None):
    convos, next_cursor = await pg.list_conversations(
        owner_id=owner_id,
        client_id=client_id,
        limit=limit,
        cursor=cursor,
    )
    return ConversationListResponse(
        conversations=[ConversationSummary(**c) for c in convos],
        next_cursor=next_cursor,
    )


@app.post(
    "/v1/conversations/resolve",
    response_model=ConversationResolveResponse,
    tags=["conversations"],
    dependencies=[Depends(require_api_key)],
    summary="Resolve rolling conversation for a client (reuse if recently active)",
)
async def resolve_conversation(body: ConversationResolveRequest):
    cid, reused = await pg.resolve_conversation(
        owner_id=body.owner_id,
        client_id=body.client_id,
        idle_ttl_s=body.idle_ttl_s,
        title=body.title,
    )
    return ConversationResolveResponse(conversation_id=str(cid), reused=reused)


# -------------------------
# Messages
# -------------------------

@app.post(
    "/v1/conversations/{conversation_id}/messages",
    response_model=MessageCreateResponse,
    tags=["messages"],
    dependencies=[Depends(require_api_key)],
    summary="Append a message (and index it for retrieval when applicable)",
)
async def add_message(conversation_id: str, body: MessageCreateRequest):
    cid = UUID(conversation_id)

    mid = await pg.add_message(
        conversation_id=cid,
        owner_id=body.owner_id,
        role=body.role,
        content=body.content,
        client_id=body.client_id,
        metadata=body.metadata,
    )

    if body.role in ("user", "assistant") and should_index_message(body.role, body.content):
        try:
            await qdrant.upsert_message_vector(
                message_id=mid,
                owner_id=body.owner_id,
                conversation_id=cid,
                role=body.role,
                content=body.content,
                client_id=body.client_id,
            )
        except Exception:
            logging.exception(
                "qdrant upsert failed (non-fatal)",
                extra={"message_id": str(mid)},
            )

    return MessageCreateResponse(message_id=str(mid))


@app.post(
    "/v1/events/ingest",
    response_model=EventIngestResponse,
    tags=["events"],
    dependencies=[Depends(require_api_key)],
    summary="Ingest one external event as a durable event memory",
)
async def ingest_event(body: EventIngestRequest, request: Request):
    _require_matching_request_id(request, body.request_id)

    source_type, source_type_original, stream_key = _normalize_source_type(body.source_type)
    event_log, created = await pg.claim_event_ingest(
        owner_id=body.owner_id,
        source_type=source_type,
        source_event_id=body.source_event_id,
        event_type=body.event_type,
        event_time=body.event_time.isoformat() if body.event_time else None,
        payload_json=body.payload_json,
    )
    if (not created) and event_log.get("message_id"):
        return EventIngestResponse(
            request_id=body.request_id,
            created=False,
            event_log_id=event_log["event_log_id"],
            conversation_id=event_log.get("conversation_id"),
            message_id=event_log.get("message_id"),
            entity_ids=[],
        )

    conversation_id = await pg.get_or_create_event_stream_conversation(
        owner_id=body.owner_id,
        client_id=stream_key,
        title=_event_stream_title(source_type),
    )
    metadata: dict[str, Any] = {
        "memory_kind": "event",
        "event_memory": True,
        "source_type": source_type,
        "source_stream": stream_key,
        "source_event_id": body.source_event_id,
        "event_type": body.event_type,
        "event_log_id": event_log["event_log_id"],
        "payload_json": body.payload_json,
        "entities": [entity.model_dump() for entity in body.entities],
    }
    if body.event_time is not None:
        metadata["event_time"] = body.event_time.isoformat()
    if source_type_original is not None:
        metadata["source_type_original"] = source_type_original

    content = _render_event_message_content(
        source_type=source_type,
        event_type=body.event_type,
        source_event_id=body.source_event_id,
        event_time=body.event_time,
        payload_json=body.payload_json,
    )
    message_id = await pg.add_message(
        conversation_id=conversation_id,
        owner_id=body.owner_id,
        role="tool",
        content=content,
        client_id=stream_key,
        metadata=metadata,
    )
    await pg.finalize_event_ingest(
        event_log_id=UUID(event_log["event_log_id"]),
        conversation_id=conversation_id,
        message_id=message_id,
    )

    try:
        await qdrant.upsert_message_vector(
            message_id=message_id,
            owner_id=body.owner_id,
            conversation_id=conversation_id,
            role="tool",
            content=content,
            client_id=stream_key,
        )
    except Exception:
        logging.exception(
            "qdrant upsert failed for event message (non-fatal)",
            extra={"message_id": str(message_id)},
        )

    entity_ids: list[str] = []
    for entity in body.entities:
        row = await pg.upsert_memory_entity(
            owner_id=body.owner_id,
            entity_type=entity.entity_type,
            canonical_name=entity.canonical_name,
            metadata=entity.metadata,
        )
        entity_ids.append(row["entity_id"])

    return EventIngestResponse(
        request_id=body.request_id,
        created=True,
        event_log_id=event_log["event_log_id"],
        conversation_id=str(conversation_id),
        message_id=str(message_id),
        entity_ids=entity_ids,
    )


# -------------------------
# Artifacts
# -------------------------

@app.post(
    "/v1/ingestion/files",
    response_model=FileIngestionResponse,
    tags=["ingestion"],
    dependencies=[Depends(require_api_key)],
    summary="Ingest local files or directories into artifact chunks",
)
async def ingest_files_endpoint(body: FileIngestionRequest):
    if not body.paths:
        raise HTTPException(status_code=422, detail="paths must not be empty")
    result = await ingest_files(
        pg=pg,
        qdrant=qdrant,
        settings=settings,
        owner_id=body.owner_id,
        client_id=body.client_id,
        source_surface=body.source_surface,
        repo_name=body.repo_name,
        paths=body.paths,
    )
    return FileIngestionResponse(**result)

@app.post(
    "/v1/artifacts/init",
    response_model=ArtifactInitResponse,
    tags=["artifacts"],
    dependencies=[Depends(require_api_key)],
    summary="Initialize artifact upload and return upload URL",
)
async def init_artifact(body: ArtifactInitRequest):
    try:
        conversation_id = UUID(body.conversation_id) if body.conversation_id else None
    except ValueError:
        raise HTTPException(status_code=400, detail="conversation_id must be a UUID")
    allowed_mime = {item.strip() for item in settings.artifacts_allowed_mime.split(",") if item.strip()}
    if body.mime not in allowed_mime:
        raise HTTPException(status_code=422, detail=f"mime '{body.mime}' is not allowed")
    if body.size > settings.artifacts_max_size_bytes:
        raise HTTPException(status_code=413, detail="artifact size exceeds configured limit")

    artifact_id = uuid4()
    object_uri = build_artifact_object_uri(body.owner_id, artifact_id, body.filename)

    row = await pg.create_artifact(
        artifact_id=artifact_id,
        owner_id=body.owner_id,
        client_id=body.client_id,
        conversation_id=conversation_id,
        filename=body.filename,
        mime=body.mime,
        size=body.size,
        object_uri=object_uri,
        source_surface=body.source_surface,
    )

    upload_url = build_artifact_transfer_url("upload", row["artifact_id"])
    if settings.object_store_enabled:
        try:
            upload_url = object_store.create_presigned_put_url(
                key=row["object_uri"],
                content_type=row["mime"],
                expires_s=settings.artifacts_presign_ttl_s,
            )
        except Exception as e:
            logging.warning("object store init failed", extra={"artifact_id": row["artifact_id"], "error_class": type(e).__name__})
            raise HTTPException(status_code=503, detail="artifact upload unavailable")

    return ArtifactInitResponse(
        artifact_id=row["artifact_id"],
        upload_url=upload_url,
        upload_url_expires_in_s=settings.artifacts_presign_ttl_s,
        object_uri=row["object_uri"],
        status=row["status"],
    )


@app.post(
    "/v1/artifacts/complete",
    response_model=ArtifactResponse,
    tags=["artifacts"],
    dependencies=[Depends(require_api_key)],
    summary="Mark artifact upload complete",
)
async def complete_artifact(body: ArtifactCompleteRequest):
    try:
        artifact_id = UUID(body.artifact_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="artifact_id must be a UUID")

    existing = await pg.get_artifact(artifact_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="artifact_id not found")
    if body.owner_id is not None and existing.get("owner_id") != body.owner_id:
        raise HTTPException(status_code=404, detail="artifact_id not found")

    should_derive_text = False
    if body.status == "completed" and settings.object_store_enabled:
        try:
            meta = object_store.head_object(existing["object_uri"])
        except Exception as e:
            logging.warning("object store artifact validation failed", extra={"artifact_id": existing["artifact_id"], "error_class": type(e).__name__})
            raise HTTPException(status_code=503, detail="artifact object validation unavailable")
        if meta is None:
            raise HTTPException(status_code=409, detail="artifact object is missing in object store")
        if int(meta.size) != int(existing["size"]):
            raise HTTPException(status_code=409, detail="artifact size mismatch with object store")
        should_derive_text = (
            is_supported_text_artifact_mime(existing.get("mime"))
            and int(existing["size"]) <= int(getattr(settings, "artifact_text_derivation_max_bytes", settings.ingest_max_file_bytes))
        )

    if should_derive_text:
        try:
            raw = object_store.read_object_bytes(
                existing["object_uri"],
                max_bytes=int(getattr(settings, "artifact_text_derivation_max_bytes", settings.ingest_max_file_bytes)),
            )
            text = raw.decode("utf-8")
            await derive_text_chunks_for_artifact(
                pg=pg,
                qdrant=qdrant,
                settings=settings,
                artifact=existing,
                text=text,
                derivation_version=TEXT_ARTIFACT_DERIVATION_VERSION,
                file_path=existing.get("file_path") or existing.get("filename"),
                repo_name=existing.get("repo_name"),
            )
        except UnicodeDecodeError:
            raise HTTPException(status_code=422, detail="artifact text derivation requires UTF-8 content")
        except Exception as e:
            logging.warning("artifact text derivation failed", extra={"artifact_id": existing["artifact_id"], "error_class": type(e).__name__})
            raise HTTPException(status_code=503, detail="artifact text derivation failed")

    row = await pg.complete_artifact(
        artifact_id=artifact_id,
        status=body.status,
        sha256=body.sha256,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="artifact_id not found")

    download_url = build_artifact_transfer_url("download", row["artifact_id"])
    if settings.object_store_enabled:
        try:
            download_url = object_store.create_presigned_get_url(
                key=row["object_uri"],
                expires_s=settings.artifacts_presign_ttl_s,
            )
        except Exception as e:
            logging.warning("object store download URL generation failed", extra={"artifact_id": row["artifact_id"], "error_class": type(e).__name__})
            raise HTTPException(status_code=503, detail="artifact download unavailable")

    return ArtifactResponse(
        **row,
        download_url=download_url,
        download_url_expires_in_s=settings.artifacts_presign_ttl_s,
    )


@app.get(
    "/v1/artifacts/{artifact_id}",
    response_model=ArtifactResponse,
    tags=["artifacts"],
    dependencies=[Depends(require_api_key)],
    summary="Get artifact metadata",
)
async def get_artifact(artifact_id: str):
    try:
        aid = UUID(artifact_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="artifact_id must be a UUID")

    row = await pg.get_artifact(aid)
    if row is None:
        raise HTTPException(status_code=404, detail="artifact_id not found")

    download_url = build_artifact_transfer_url("download", row["artifact_id"])
    if settings.object_store_enabled:
        try:
            download_url = object_store.create_presigned_get_url(
                key=row["object_uri"],
                expires_s=settings.artifacts_presign_ttl_s,
            )
        except Exception as e:
            logging.warning("object store download URL generation failed", extra={"artifact_id": row["artifact_id"], "error_class": type(e).__name__})
            raise HTTPException(status_code=503, detail="artifact download unavailable")

    return ArtifactResponse(
        **row,
        download_url=download_url,
        download_url_expires_in_s=settings.artifacts_presign_ttl_s,
    )


# -------------------------
# Retrieval
# -------------------------

@app.post(
    "/v1/retrieve",
    response_model=RetrieveResponse,
    tags=["retrieve"],
    dependencies=[Depends(require_api_key)],
    summary="Retrieve relevant past messages",
)
async def retrieve(body: RetrieveRequest, request: Request):
    hits = await qdrant.search(
        owner_id=body.owner_id,
        query=body.query,
        k=body.k,
        min_score=body.min_score,
        conversation_id=body.conversation_id,
        client_id=body.client_id,
        exclude_message_ids=body.exclude_message_ids,
    )

    ids = _safe_uuid_message_ids(hits, context="/v1/retrieve", kind="retrieve")
    snippets = await pg.get_message_snippets_by_ids(ids)

    score_by_id = {h.message_id: h.score for h in hits}
    out: list[RetrieveHit] = []
    for s in snippets:
        out.append(
            RetrieveHit(
                message_id=s["message_id"],
                conversation_id=s["conversation_id"],
                role=s["role"],
                content=s["content"],
                created_at=s["created_at"],
                score=score_by_id.get(s["message_id"]),
            )
        )

    return RetrieveResponse(hits=out)


@app.post(
    "/v1/conversations/{conversation_id}/retrieve",
    response_model=TieredRetrieveResponse,
    tags=["retrieve"],
    dependencies=[Depends(require_api_key)],
    summary="Tier-aware retrieval for a specific conversation (v1 contract)",
)
async def retrieve_tiered(conversation_id: str, body: TieredRetrieveRequest):
    try:
        cid = UUID(conversation_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="conversation_id must be a UUID")

    if not await pg.conversation_exists(cid):
        raise HTTPException(status_code=404, detail="conversation_id not found")

    semantic_hits = await qdrant.search(
        owner_id=body.owner_id,
        query=body.query,
        k=body.k,
        min_score=body.min_score,
        conversation_id=cid,
        client_id=body.client_id,
    )
    semantic_ids = _safe_uuid_message_ids(
        semantic_hits,
        context="/v1/conversations/{id}/retrieve",
        kind="semantic",
    )
    semantic_snips = await pg.get_message_snippets_by_ids(semantic_ids)
    semantic_score_by_id = {h.message_id: h.score for h in semantic_hits}
    working_snips = await pg.get_recent_message_snippets(conversation_id=cid, limit=body.working_limit)
    pinned_items = await pg.get_pinned_memories(owner_id=body.owner_id, conversation_id=cid, limit=body.pinned_limit)
    policy_items = await pg.get_policy_overlays(owner_id=body.owner_id, surface=body.surface)
    persona_items = await pg.get_persona_overlays(owner_id=body.owner_id, surface=body.surface)

    return TieredRetrieveResponse(
        conversation_id=str(cid),
        query=body.query,
        working=[
            RetrieveHit(
                message_id=s["message_id"],
                conversation_id=s["conversation_id"],
                role=s["role"],
                content=s["content"],
                created_at=s["created_at"],
                score=None,
            )
            for s in working_snips
        ],
        semantic=[
            RetrieveHit(
                message_id=s["message_id"],
                conversation_id=s["conversation_id"],
                role=s["role"],
                content=s["content"],
                created_at=s["created_at"],
                score=semantic_score_by_id.get(s["message_id"]),
            )
            for s in semantic_snips
        ],
        pinned=[OverlayItem(**item) for item in pinned_items],
        policy=[OverlayItem(**item) for item in policy_items],
        persona=[OverlayItem(**item) for item in persona_items],
    )


@app.post(
    "/v2/conversations/{conversation_id}/retrieve",
    response_model=RetrieveBundleResponse,
    tags=["retrieve"],
    dependencies=[Depends(require_api_key)],
    summary="Retrieve minimal context bundle for a specific conversation (v2 contract)",
)
async def retrieve_tiered_v2(conversation_id: str, body: RetrieveBundleRequest, request: Request):
    _require_matching_request_id(request, body.request_id)

    try:
        cid = UUID(conversation_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="conversation_id must be a UUID")

    convo = await pg.get_conversation(cid)
    if convo is None or convo.get("owner_id") != body.owner_id:
        raise HTTPException(status_code=404, detail="conversation_id not found")

    opts = body.retrieval or RetrievalOptions(k=settings.retrieval_k, min_score=0.25, scope="conversation")
    include_artifacts = True if body.include_artifacts is None else body.include_artifacts
    try:
        payload = await build_retrieval_response_payload(
            pg=pg,
            qdrant=qdrant,
            settings=settings,
            request_id=body.request_id,
            owner_id=body.owner_id,
            conversation_id=cid,
            client_id=convo.get("client_id"),
            query=body.query,
            opts=opts,
            mode=body.mode,
            include_artifacts=include_artifacts,
            allowed_memory_domains=body.allowed_memory_domains,
            blocked_memory_domains=body.blocked_memory_domains,
        )
    except Exception:
        diagnostics = doctrine_diagnostics_for_bundle(
            request_id=body.request_id,
            conversation_id=str(cid),
            owner_id=body.owner_id,
            mode=body.mode,
            status="failed",
            error="canonical_retrieval_failed",
        )
        if getattr(settings, "enable_trace_storage", False):
            try:
                await pg.create_trace(
                    {
                        "request_id": body.request_id,
                        "conversation_id": cid,
                        "owner_id": body.owner_id,
                        "client_id": convo.get("client_id"),
                        "surface": "bms-retrieval",
                        "profile": {},
                        "retrieval": diagnostics,
                        "prompt": {},
                        "router_decision": {},
                        "manual_override": {},
                        "model_call": {},
                        "model_calls": [],
                        "fallback": {"status": "failed", "reason": "canonical_retrieval_failed"},
                        "artifacts": {},
                        "references": [],
                        "cost": {},
                        "status": "failed",
                        "error": "canonical_retrieval_failed",
                    }
                )
            except Exception:
                logging.exception("retrieval diagnostic trace write failed", extra={"request_id": body.request_id})
        raise HTTPException(
            status_code=503,
            detail={
                "error": "canonical_retrieval_failed",
                "request_id": body.request_id,
                "mode": body.mode,
            },
        )

    if getattr(settings, "enable_trace_storage", False):
        try:
            await pg.create_trace(
                {
                    "request_id": body.request_id,
                    "conversation_id": cid,
                    "owner_id": body.owner_id,
                    "client_id": convo.get("client_id"),
                    "surface": "bms-retrieval",
                    "profile": {},
                    "retrieval": payload["diagnostics"],
                    "prompt": {},
                    "router_decision": {},
                    "manual_override": {},
                    "model_call": {},
                    "model_calls": [],
                    "fallback": {
                        "fallback_to_raw": payload["diagnostics"].get("fallback_to_raw", False),
                        "reasons": payload["diagnostics"].get("fallback_reasons", []),
                    },
                    "artifacts": {},
                    "references": [],
                    "cost": {},
                    "status": payload["diagnostics"].get("status", "ok"),
                }
            )
        except Exception:
            logging.exception("retrieval diagnostic trace write failed", extra={"request_id": body.request_id})

    return RetrieveBundleResponse(
        request_id=body.request_id,
        conversation_id=str(cid),
        **payload,
    )


# -------------------------
# Hygiene
# -------------------------

@app.post(
    "/v1/hygiene/scan",
    response_model=HygieneScanResponse,
    tags=["hygiene"],
    dependencies=[Depends(require_api_key)],
    summary="Run a minimal pinned-memory hygiene scan (redundancy plus metadata-shaped contradiction checks)",
)
async def scan_hygiene(body: HygieneScanRequest):
    if not settings.enable_hygiene_scan_api:
        raise HTTPException(status_code=503, detail="hygiene scan API is disabled")

    pinned_rows = await pg.get_pinned_memories_for_hygiene(owner_id=body.owner_id, limit=body.limit)
    seen_by_text: dict[str, dict[str, Any]] = {}
    created_flags: list[dict[str, Any]] = []
    by_topic: dict[str, list[dict[str, Any]]] = {}

    for row in pinned_rows:
        normalized = _normalize_hygiene_text(row["content"])
        if normalized in seen_by_text:
            created_flags.append(
                await pg.create_hygiene_flag(
                    owner_id=body.owner_id,
                    subject_type="pinned_memory",
                    subject_id=UUID(row["id"]),
                    flag_type="pinned_redundancy",
                    details={"duplicate_of": seen_by_text[normalized]["id"]},
                )
            )
        else:
            seen_by_text[normalized] = row

        metadata = row.get("metadata") or {}
        # Current MVP contradiction detection only applies when pinned-memory metadata
        # explicitly provides comparable topic/value fields. Rows without that shape are ignored.
        topic = metadata.get("topic")
        value = metadata.get("value")
        if isinstance(topic, str) and isinstance(value, str):
            by_topic.setdefault(topic.strip().lower(), []).append({"id": row["id"], "value": value.strip().lower()})

    for topic, items in by_topic.items():
        values = {item["value"] for item in items}
        if len(values) > 1:
            for item in items:
                created_flags.append(
                    await pg.create_hygiene_flag(
                        owner_id=body.owner_id,
                        subject_type="pinned_memory",
                        subject_id=UUID(item["id"]),
                        flag_type="pinned_contradiction",
                        details={"topic": topic, "values_seen": sorted(values)},
                    )
                )

    return HygieneScanResponse(
        owner_id=body.owner_id,
        flags_created=sum(1 for flag in created_flags if flag.get("created")),
        flags=[HygieneFlagItem(**flag) for flag in created_flags],
    )


@app.get(
    "/v1/hygiene/flags",
    response_model=HygieneFlagListResponse,
    tags=["hygiene"],
    dependencies=[Depends(require_api_key)],
    summary="List memory hygiene flags",
)
async def list_hygiene_flags(owner_id: str, status: str | None = None, limit: int = 50):
    flags = await pg.list_hygiene_flags(owner_id=owner_id, status=status, limit=limit)
    return HygieneFlagListResponse(flags=[HygieneFlagItem(**flag) for flag in flags])


# -------------------------
# Chat
# -------------------------

async def _run_chat(
    body: ChatRequest,
    request: Request,
    *,
    surface: str | None = None,
    artifact_ids: list[str] | None = None,
    surface_behavior: dict[str, Any] | None = None,
) -> ChatResponse:
    request_started = time.perf_counter()
    owner_id = body.owner_id
    client_id = body.client_id

    created_new = False

    if not body.conversation_id:
        conversation_id = str(await pg.create_conversation(owner_id=owner_id, client_id=client_id, title=None))
        created_new = True
    else:
        conversation_id = body.conversation_id

    try:
        cid = UUID(conversation_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="conversation_id must be a UUID")

    if not created_new and not await pg.conversation_exists(cid):
        raise HTTPException(status_code=404, detail="conversation_id not found")

    inserted_user_message_ids: set[str] = set()

    last_user_text: str | None = None
    for m in body.messages:
        if m.role != "user":
            continue
        last_user_text = m.content
        mid = await pg.add_message(
            conversation_id=cid,
            owner_id=owner_id,
            role="user",
            content=m.content,
            client_id=client_id,
            metadata=None,
        )
        inserted_user_message_ids.add(str(mid))
        if should_index_message("user", m.content):
            try:
                await qdrant.upsert_message_vector(
                    message_id=mid,
                    owner_id=owner_id,
                    conversation_id=cid,
                    role="user",
                    content=m.content,
                    client_id=client_id,
                )
            except Exception:
                logging.exception(
                    "qdrant upsert failed for user message (non-fatal)",
                    extra={
                        "message_id": str(mid),
                        "request_id": getattr(request.state, "request_id", None),
                    },
                )

    if not last_user_text:
        raise HTTPException(status_code=400, detail="At least one user message is required.")

    opts = body.retrieval or RetrievalOptions(k=settings.retrieval_k, min_score=0.25, scope="conversation")
    k = opts.k
    min_score = opts.min_score
    artifact_k = _retrieval_artifact_k()

    scope_used = opts.scope
    fallback_used = False

    def _scope_filters(scope: str) -> tuple[str | None, str | None]:
        if scope == "conversation":
            return str(cid), None
        if scope == "client":
            return None, client_id
        return None, None

    async def _run_search(scope: str, min_score_: float) -> list[QdrantHit]:
        conv_filter, client_filter = _scope_filters(scope)
        return await qdrant.search(
            owner_id=owner_id,
            query=last_user_text,
            k=k,
            min_score=min_score_,
            conversation_id=conv_filter,
            client_id=client_filter,
            exclude_message_ids=list(inserted_user_message_ids) if inserted_user_message_ids else None,
        )

    try:
        retrieval_hits = await _run_search(opts.scope, min_score)
    except Exception:
        logging.exception("qdrant search failed (non-fatal)")
        retrieval_hits = []

    if opts.scope == "conversation" and (len(retrieval_hits) == 0 or len(retrieval_hits) < max(2, k // 2)):
        owner_min_score = min(1.0, min_score + 0.05)
        try:
            retrieval_hits = await _run_search("owner", owner_min_score)
            fallback_used = True
            scope_used = "owner"
        except Exception:
            logging.exception("qdrant owner-scope fallback search failed (non-fatal)")
            retrieval_hits = []

    filtered_hits = [h for h in retrieval_hits if h.message_id not in inserted_user_message_ids]
    retrieval_ids = _safe_uuid_message_ids(filtered_hits, context="/v1/chat", kind="retrieval")
    retrieved = await pg.get_message_snippets_by_ids(retrieval_ids)
    artifact_hits = await qdrant.search_artifact_chunks(
        owner_id=owner_id,
        query=last_user_text,
        k=artifact_k,
        min_score=min_score,
        client_id=client_id if opts.scope == "client" else None,
        conversation_id=cid if opts.scope == "conversation" else None,
    ) if artifact_k > 0 else []
    artifact_ids_for_prompt = _safe_uuid_ids(
        [hit.derived_text_id for hit in artifact_hits],
        context="/v1/chat",
        kind="artifact",
    )
    artifact_snips = await pg.get_derived_text_snippets_by_ids(artifact_ids_for_prompt)
    recent = await pg.get_recent_messages(conversation_id=cid, limit=settings.recent_turns)

    system_preamble = (
        "You are a helpful assistant.\n"
        "- Use the provided context when relevant.\n"
        "- If context conflicts, prefer newer timestamps.\n"
        "- Do not invent facts.\n"
    )
    message_context_block = build_context_block(retrieved=retrieved, max_chars=settings.max_context_chars)
    artifact_context_block = build_artifact_context_block(
        [
            {
                "repo_name": s.get("repo_name"),
                "file_path": s.get("file_path"),
                "snippet": _cap_snippet(s["text"], _retrieval_artifact_max_snippet_chars()),
            }
            for s in artifact_snips[:artifact_k]
        ],
        max_chars=max(1000, settings.max_context_chars // 3),
    )
    context_block = "\n\n".join(part for part in (message_context_block, artifact_context_block) if part)
    prompt_messages = assemble_messages(
        system_preamble=system_preamble,
        context_block=context_block,
        recent_messages=recent,
        user_messages=[m.model_dump() for m in body.messages],
    )

    model_started = time.perf_counter()
    try:
        answer = await litellm.chat(
            model=settings.chat_model,
            messages=prompt_messages,
            temperature=settings.chat_temperature,
            request_id=getattr(request.state, "request_id", None),
        )
    except Exception as e:
        logging.exception(
            "LiteLLM chat call failed",
            extra={"request_id": getattr(request.state, "request_id", None)},
        )
        raise HTTPException(status_code=502, detail=str(e))
    model_latency_ms = int((time.perf_counter() - model_started) * 1000)

    amid = await pg.add_message(
        conversation_id=cid,
        owner_id=owner_id,
        role="assistant",
        content=answer,
        client_id=client_id,
        metadata=None,
    )
    if should_index_message("assistant", answer):
        try:
            await qdrant.upsert_message_vector(
                message_id=amid,
                owner_id=owner_id,
                conversation_id=cid,
                role="assistant",
                content=answer,
                client_id=client_id,
            )
        except Exception:
            logging.exception(
                "qdrant upsert failed for assistant message (non-fatal)",
                extra={
                    "message_id": str(amid),
                    "request_id": getattr(request.state, "request_id", None),
                },
            )

    debug_block: RetrievalDebug | None = None
    if getattr(body, "debug", False):
        debug_block = RetrievalDebug(
            scope_used=scope_used,
            fallback_used=fallback_used,
            hits=[RetrievalDebugHit(message_id=h.message_id, score=h.score) for h in filtered_hits],
        )

    resp = ChatResponse(
        conversation_id=str(cid),
        answer=answer,
        retrieved_count=len(retrieved) + len(artifact_snips[:artifact_k]),
        debug=debug_block,
    )

    request_id = getattr(request.state, "request_id", None)
    if request_id:
        try:
            await pg.create_trace(
                {
                    "request_id": request_id,
                    "conversation_id": cid,
                    "owner_id": owner_id,
                    "client_id": client_id,
                    "surface": surface or "chat",
                    "profile": (
                        {
                            "surface_context": {
                                "surface_type": surface_behavior["surface_type"],
                                "interaction_mode": surface_behavior["interaction_mode"],
                                "spoken_output": surface_behavior["spoken_output"],
                                "active_task_mode": surface_behavior["active_task_mode"],
                                "latency_preference": surface_behavior["latency_preference"],
                                "verbosity_target": surface_behavior["verbosity_target"],
                                "allows_expansion": surface_behavior["allows_expansion"],
                                "output_format": surface_behavior["output_format"],
                            },
                            **(
                                {"surface_compatibility_note": surface_behavior["compatibility_note"]}
                                if surface_behavior["compatibility_note"]
                                else {}
                            ),
                        }
                        if surface_behavior
                        else {}
                    ),
                    "router_decision": {
                    "selected_model": settings.chat_model,
                    "rule_id": "default-chat-model",
                    "fallbacks": [],
                    },
                    "retrieval": {
                    "query": last_user_text,
                    "scope_requested": opts.scope,
                    "scope_used": scope_used,
                    "fallback_used": fallback_used,
                    "hits": [{"message_id": h.message_id, "score": h.score} for h in filtered_hits],
                    "artifacts_used": (artifact_ids or []) + [s["artifact_id"] for s in artifact_snips[:artifact_k]],
                    "artifact_refs": [
                        {
                            "artifact_id": s["artifact_id"],
                            "file_path": s["file_path"],
                            "snippet": _cap_snippet(s["text"], _retrieval_artifact_max_snippet_chars()),
                            "relevance_score": next((h.score for h in artifact_hits if h.derived_text_id == s["derived_text_id"]), None),
                            "repo_name": s.get("repo_name"),
                        }
                        for s in artifact_snips[:artifact_k]
                    ],
                    },
                    "manual_override": {},
                    "model_call": {
                    "provider": "litellm",
                    "model": settings.chat_model,
                    "latency_ms": model_latency_ms,
                    "error": None,
                    },
                    "fallback": {},
                    "cost": {"estimate_usd": None},
                    "latency_ms": int((time.perf_counter() - request_started) * 1000),
                    "status": "ok",
                    "error": None,
                }
            )
        except Exception:
            logging.exception("trace write failed (non-fatal)", extra={"request_id": request_id})

    return resp


@app.post(
    "/v1/chat",
    response_model=ChatResponse,
    tags=["chat"],
    dependencies=[Depends(require_api_key)],
    summary="Chat with retrieval-augmented memory",
)
async def chat(body: ChatRequest, request: Request):
    resp = await _run_chat(body, request)
    return resp


@app.post(
    "/v1/orchestrate/chat",
    response_model=OrchestrateChatResponse,
    tags=["chat"],
    dependencies=[Depends(require_api_key)],
    summary="Surface-aware orchestration entrypoint (additive wrapper over /v1/chat)",
)
async def orchestrate_chat(body: OrchestrateChatRequest, request: Request):
    surface_behavior = _resolve_surface_behavior(body.surface, body.surface_context)
    base_req = ChatRequest(
        owner_id=body.owner_id,
        conversation_id=body.conversation_id,
        client_id=body.client_id,
        messages=body.messages,
        retrieval=body.retrieval,
        debug=body.debug,
    )
    resp = await _run_chat(
        base_req,
        request,
        surface=surface_behavior["surface_type"],
        artifact_ids=body.artifact_ids or [],
        surface_behavior=surface_behavior,
    )
    return OrchestrateChatResponse(**resp.model_dump(), request_id=(getattr(request.state, "request_id", None) or ""))


@app.get(
    "/v1/proactive/preferences",
    response_model=ProactivePrefsResponse,
    tags=["proactive"],
    dependencies=[Depends(require_api_key)],
    summary="Get proactive preferences for an owner",
)
async def get_proactive_preferences(owner_id: str):
    row = await pg.get_proactive_prefs(owner_id)
    if row is None:
        return ProactivePrefsResponse(
            owner_id=owner_id,
            enabled=False,
            allowed_surfaces_json=[],
            rule_prefs_json={},
            created_at=None,
            updated_at=None,
        )
    return ProactivePrefsResponse(**row)


@app.put(
    "/v1/proactive/preferences",
    response_model=ProactivePrefsResponse,
    tags=["proactive"],
    dependencies=[Depends(require_api_key)],
    summary="Create or update proactive preferences for an owner",
)
async def put_proactive_preferences(body: ProactivePrefsUpdateRequest):
    row = await pg.upsert_proactive_prefs(
        owner_id=body.owner_id,
        enabled=body.enabled,
        allowed_surfaces_json=list(body.allowed_surfaces_json),
        rule_prefs_json=body.rule_prefs_json,
    )
    logging.info("proactive_prefs_updated", extra={"owner_id": body.owner_id, "enabled": body.enabled, "allowed_surfaces": row["allowed_surfaces_json"]})
    return ProactivePrefsResponse(**row)


@app.get(
    "/v1/proactive/suggestions",
    response_model=ProactiveSuggestionListResponse,
    tags=["proactive"],
    dependencies=[Depends(require_api_key)],
    summary="List proactive suggestions with optional lifecycle status and target surface filters",
)
async def list_proactive_suggestions(
    owner_id: str,
    status: str | None = None,
    surface: str | None = None,
    delivery_status: str | None = None,
):
    rows = await pg.list_proactive_suggestions(
        owner_id=owner_id,
        status=status,
        surface=surface,
        delivery_status=delivery_status,
    )
    return ProactiveSuggestionListResponse(suggestions=[ProactiveSuggestionItem(**row) for row in rows])


@app.post(
    "/v1/proactive/suggestions/{suggestion_id}/feedback",
    response_model=ProactiveSuggestionFeedbackResponse,
    tags=["proactive"],
    dependencies=[Depends(require_api_key)],
    summary="Record user feedback for one proactive suggestion",
)
async def record_proactive_feedback(suggestion_id: str, body: ProactiveSuggestionFeedbackRequest):
    try:
        sid = UUID(suggestion_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="suggestion_id must be a UUID")

    suggestion = await pg.get_proactive_suggestion(sid)
    if suggestion is None or suggestion["owner_id"] != body.owner_id:
        raise HTTPException(status_code=404, detail="suggestion not found")

    row = await pg.record_proactive_feedback(
        suggestion_id=sid,
        owner_id=body.owner_id,
        feedback_type=body.feedback_type,
        reason=body.reason,
    )
    logging.info("proactive_feedback_recorded", extra={"owner_id": body.owner_id, "suggestion_id": suggestion_id, "feedback_type": body.feedback_type})
    return ProactiveSuggestionFeedbackResponse(**row)


@app.post(
    "/v1/proactive/suggestions/{suggestion_id}/delivery-attempt",
    response_model=ProactiveDeliveryAttemptResponse,
    tags=["proactive"],
    dependencies=[Depends(require_api_key)],
    summary="Record one delivery attempt result for a proactive suggestion",
)
async def record_proactive_delivery_attempt(suggestion_id: str, body: ProactiveDeliveryAttemptRequest):
    try:
        sid = UUID(suggestion_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="suggestion_id must be a UUID")

    row = await pg.record_proactive_delivery_attempt(
        suggestion_id=sid,
        owner_id=body.owner_id,
        surface=body.surface,
        delivery_status=body.status,
        external_id=body.external_id,
        error=body.error,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="suggestion not found")
    logging.info("proactive_delivery_attempt_recorded", extra={"owner_id": body.owner_id, "suggestion_id": suggestion_id, "surface": body.surface, "delivery_status": body.status})
    return ProactiveDeliveryAttemptResponse(**row)


@app.post(
    "/v1/initiative/evaluate",
    response_model=InitiativeEvaluateResponse,
    tags=["initiative"],
    dependencies=[Depends(require_api_key)],
    summary="Evaluate one ingested event against initiative scoring and delivery policy",
)
async def evaluate_initiative(body: InitiativeEvaluateRequest, request: Request):
    _require_matching_request_id(request, body.request_id)
    try:
        event_log_id = UUID(body.event_log_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="event_log_id must be a UUID")

    result = await evaluate_initiative_event(
        pg=pg,
        qdrant=qdrant,
        settings=settings,
        request_id=body.request_id,
        owner_id=body.owner_id,
        event_log_id=event_log_id,
        surface=body.surface,
    )
    logging.info(
        "initiative_evaluate_completed",
        extra={
            "owner_id": body.owner_id,
            "event_log_id": body.event_log_id,
            "created_count": result["created_count"],
        },
    )
    return InitiativeEvaluateResponse(**result)


@app.post(
    "/v1/initiative/feedback",
    response_model=InitiativeFeedbackResponse,
    tags=["initiative"],
    dependencies=[Depends(require_api_key)],
    summary="Record feedback for one initiative decision",
)
async def record_initiative_feedback(body: InitiativeFeedbackRequest):
    try:
        decision_id = UUID(body.decision_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="decision_id must be a UUID")

    decision = await pg.get_initiative_decision(decision_id)
    if decision is None or decision["owner_id"] != body.owner_id:
        raise HTTPException(status_code=404, detail="initiative decision not found")

    proactive_feedback_id = None
    proactive_suggestion_id = decision.get("proactive_suggestion_id")
    if proactive_suggestion_id:
        reason = body.feedback_json.get("reason") if isinstance(body.feedback_json, dict) else None
        proactive_feedback = await pg.record_proactive_feedback(
            suggestion_id=UUID(proactive_suggestion_id),
            owner_id=body.owner_id,
            feedback_type=body.feedback_type,
            reason=reason,
        )
        proactive_feedback_id = proactive_feedback["feedback_id"]

    row = await pg.record_initiative_feedback(
        decision_id=decision_id,
        proactive_feedback_id=UUID(proactive_feedback_id) if proactive_feedback_id else None,
        owner_id=body.owner_id,
        feedback_type=body.feedback_type,
        feedback_json=body.feedback_json,
    )
    logging.info(
        "initiative_feedback_recorded",
        extra={"owner_id": body.owner_id, "decision_id": body.decision_id, "feedback_type": body.feedback_type},
    )
    return InitiativeFeedbackResponse(**row)


@app.get(
    "/v1/initiative/debug/{request_id}",
    response_model=InitiativeDebugResponse,
    tags=["initiative"],
    dependencies=[Depends(require_api_key)],
    summary="Get initiative debug and explainability details by request id",
)
async def get_initiative_debug(request_id: str, owner_id: str):
    event = await pg.get_initiative_event_by_request_id(owner_id=owner_id, request_id=request_id)
    if event is None:
        return InitiativeDebugResponse(request_id=request_id)
    decisions = await pg.list_initiative_decisions(UUID(event["initiative_event_id"]))
    suggestions = []
    for decision in decisions:
        proactive_suggestion_id = decision.get("proactive_suggestion_id")
        if proactive_suggestion_id:
            suggestion = await pg.get_proactive_suggestion(UUID(proactive_suggestion_id))
            if suggestion is not None:
                suggestions.append(ProactiveSuggestionItem(**suggestion))
    feedback = await pg.list_initiative_feedback_for_event(UUID(event["initiative_event_id"]))
    return InitiativeDebugResponse(
        request_id=request_id,
        initiative_event=event,
        decisions=decisions,
        suggestions=suggestions,
        feedback=feedback,
    )


@app.get(
    "/v1/initiative/{initiative_event_id}",
    response_model=InitiativeDetailResponse,
    tags=["initiative"],
    dependencies=[Depends(require_api_key)],
    summary="Get one initiative event and its decisions",
)
async def get_initiative(initiative_event_id: str):
    try:
        iid = UUID(initiative_event_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="initiative_event_id must be a UUID")
    event = await pg.get_initiative_event(iid)
    if event is None:
        raise HTTPException(status_code=404, detail="initiative event not found")
    decisions = await pg.list_initiative_decisions(iid)
    return InitiativeDetailResponse(initiative_event=event, decisions=decisions)


@app.post(
    "/v1/internal/proactive/evaluate",
    response_model=ProactiveEvaluateResponse,
    tags=["proactive"],
    dependencies=[Depends(require_api_key)],
    summary="Evaluate one ingested event against the deterministic proactive rules",
)
async def evaluate_proactive(body: ProactiveEvaluateRequest, request: Request):
    _require_matching_request_id(request, body.request_id)
    try:
        event_log_id = UUID(body.event_log_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="event_log_id must be a UUID")

    result = await evaluate_initiative_event(
        pg=pg,
        qdrant=qdrant,
        settings=settings,
        request_id=body.request_id,
        owner_id=body.owner_id,
        event_log_id=event_log_id,
        surface=body.surface,
    )
    suggestions = result["suggestions"]
    logging.info("proactive_evaluate_completed", extra={"owner_id": body.owner_id, "event_log_id": body.event_log_id, "created_count": len(suggestions)})
    return ProactiveEvaluateResponse(
        request_id=body.request_id,
        owner_id=body.owner_id,
        event_log_id=body.event_log_id,
        created_count=len(suggestions),
        suggestions=[ProactiveSuggestionItem(**row) for row in suggestions],
    )



@app.post(
    "/v1/internal/memory/promote",
    response_model=MemoryPromoteResponse,
    tags=["memory-internal"],
    dependencies=[Depends(require_api_key)],
    summary="Manually promote or update one derived memory item",
)
async def promote_memory(body: MemoryPromoteRequest, request: Request):
    request_id = _require_matching_request_id(request, body.request_id)
    raw_source_refs = [ref.model_dump(exclude_none=True) for ref in body.source_refs]
    try:
        normalized_refs = normalize_source_refs(raw_source_refs)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    supersedes_memory_id = None
    if body.supersedes_memory_id is not None:
        try:
            supersedes_memory_id = UUID(body.supersedes_memory_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="supersedes_memory_id must be a UUID")

    try:
        result = await pg.promote_memory_item(
            owner_id=body.owner_id,
            memory_type=body.memory_type,
            summary=body.summary,
            source_refs_json=normalized_refs,
            source_ref_hash=source_ref_hash(normalized_refs),
            scores_json=normalize_scores(body.scores),
            promotion_state="promoted",
            confidence=body.confidence,
            explanation_json=body.explanation,
            generation_trace_id=body.generation_trace_id,
            expires_at=body.expires_at.isoformat() if body.expires_at else None,
            request_id=request_id,
            reinforce=body.reinforce,
            supersedes_memory_id=supersedes_memory_id,
        )
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    return MemoryPromoteResponse(
        request_id=request_id,
        memory=MemoryItemResponse(**shape_memory_item(result["memory"])),
        created=result["created"],
        updated=result["updated"],
        reinforced=result["reinforced"],
        superseded=result["superseded"],
        events_appended=result["events_appended"],
    )


@app.post(
    "/v1/internal/memory/{memory_id}/reinforce",
    response_model=MemoryItemResponse,
    tags=["memory-internal"],
    dependencies=[Depends(require_api_key)],
    summary="Manually reinforce one derived memory item",
)
async def reinforce_memory(memory_id: str, body: MemoryReinforceRequest, request: Request):
    request_id = _require_matching_request_id(request, body.request_id)
    try:
        mid = UUID(memory_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="memory_id must be a UUID")
    row = await pg.reinforce_memory_item(
        memory_id=mid,
        owner_id=body.owner_id,
        scores_json=normalize_scores(body.scores),
        reason_json=body.reason,
        request_id=request_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="memory not found")
    return MemoryItemResponse(**shape_memory_item(row))


@app.post(
    "/v1/internal/memory/{memory_id}/transition",
    response_model=MemoryTransitionResponse,
    tags=["memory-internal"],
    dependencies=[Depends(require_api_key)],
    summary="Transition one derived memory item and append bounded audit evidence",
)
async def transition_memory(memory_id: str, body: MemoryTransitionRequest, request: Request):
    request_id = _require_matching_request_id(request, body.request_id)
    try:
        mid = UUID(memory_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="memory_id must be a UUID")

    related_memory_id = None
    if body.related_memory_id is not None:
        try:
            related_memory_id = UUID(body.related_memory_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="related_memory_id must be a UUID")

    try:
        result = await pg.transition_memory_item(
            memory_id=mid,
            owner_id=body.owner_id,
            new_status=body.status,
            reason_code=body.reason.code,
            reason_metadata=body.reason.metadata,
            request_id=request_id,
            related_memory_id=related_memory_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if result is None:
        raise HTTPException(status_code=404, detail="memory not found")
    return MemoryTransitionResponse(
        request_id=request_id,
        changed=result["changed"],
        memory=MemoryItemResponse(**shape_memory_item(result["memory"])),
        events_appended=result["events_appended"],
    )


@app.get(
    "/v1/internal/memory/{memory_id}/debug",
    response_model=MemoryDebugResponse,
    tags=["memory-internal"],
    dependencies=[Depends(require_api_key)],
    summary="Inspect one derived memory item and its audit events",
)
async def debug_memory(memory_id: str, owner_id: str):
    try:
        mid = UUID(memory_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="memory_id must be a UUID")
    debug = await pg.get_memory_debug(mid, owner_id)
    if debug is None:
        raise HTTPException(status_code=404, detail="memory not found")
    return MemoryDebugResponse(
        memory=MemoryItemResponse(**shape_memory_item(debug["memory"])),
        events=[MemoryEventItem(**shape_memory_event(event)) for event in debug["events"]],
    )



@app.post(
    "/v1/internal/episodes",
    response_model=EpisodeCreateResponse,
    tags=["episodes-internal"],
    dependencies=[Depends(require_api_key)],
    summary="Manually create or update one derived episode",
)
async def create_episode(body: EpisodeCreateRequest, request: Request):
    request_id = _require_matching_request_id(request, body.request_id)
    raw_source_refs = [ref.model_dump(exclude_none=True) for ref in body.source_refs]
    try:
        normalized_refs = normalize_episode_source_refs(raw_source_refs)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    trigger = normalize_json_map(body.trigger)
    time_window = normalize_json_map(body.time_window)
    source_hash = episode_source_ref_hash(normalized_refs)
    result = await pg.create_or_update_episode(
        owner_id=body.owner_id,
        title=body.title,
        summary=body.summary,
        episode_type=body.episode_type,
        trigger_json=trigger,
        outcome=body.outcome,
        significance=body.significance,
        unresolved_json=normalize_json_map(body.unresolved),
        source_refs_json=normalized_refs,
        source_ref_hash=source_hash,
        episode_key=episode_key(
            episode_type=body.episode_type,
            source_ref_hash_value=source_hash,
            trigger_json=trigger,
            time_window_json=time_window,
        ),
        callback_candidates_json=normalize_json_list(body.callback_candidates),
        time_window_json=time_window,
        participants_json=normalize_json_list(body.participants),
        confidence=body.confidence,
        explanation_json=normalize_json_map(body.explanation),
        generation_trace_id=body.generation_trace_id,
        request_id=request_id,
        derivation_version=EPISODE_DERIVATION_VERSION,
    )
    return EpisodeCreateResponse(
        request_id=request_id,
        episode=EpisodeItemResponse(**shape_episode(result["episode"])),
        created=result["created"],
        updated=result["updated"],
    )


@app.post(
    "/v1/internal/episodes/{episode_id}/links",
    response_model=EpisodeLinkResponse,
    tags=["episodes-internal"],
    dependencies=[Depends(require_api_key)],
    summary="Manually create explicit episode links",
)
async def create_episode_links(episode_id: str, body: EpisodeLinkRequest, request: Request):
    request_id = _require_matching_request_id(request, body.request_id)
    try:
        eid = UUID(episode_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="episode_id must be a UUID")
    result = await pg.create_episode_links(
        episode_id=eid,
        owner_id=body.owner_id,
        links=[link.model_dump() for link in body.links],
        request_id=request_id,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="episode not found")
    return EpisodeLinkResponse(
        request_id=request_id,
        episode_id=result["episode_id"],
        created_count=result["created_count"],
        existing_count=result["existing_count"],
        links=[EpisodeLinkItem(**shape_episode_link(link)) for link in result["links"]],
    )


@app.get(
    "/v1/internal/episodes/{episode_id}/debug",
    response_model=EpisodeDebugResponse,
    tags=["episodes-internal"],
    dependencies=[Depends(require_api_key)],
    summary="Inspect one derived episode, its links, and lifecycle events",
)
async def debug_episode(episode_id: str, owner_id: str):
    try:
        eid = UUID(episode_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="episode_id must be a UUID")
    debug = await pg.get_episode_debug(eid, owner_id)
    if debug is None:
        raise HTTPException(status_code=404, detail="episode not found")
    return EpisodeDebugResponse(
        episode=EpisodeItemResponse(**shape_episode(debug["episode"])),
        links=[EpisodeLinkItem(**shape_episode_link(link)) for link in debug["links"]],
        events=[EpisodeEventItem(**shape_episode_event(event)) for event in debug["events"]],
    )


@app.get(
    "/v1/internal/derived/{derivative_class}/{derived_id}",
    response_model=DerivedInspectionResponse,
    tags=["derived-internal"],
    dependencies=[Depends(require_api_key)],
    summary="Inspect one owner-scoped derivative through the bounded shared contract",
)
async def inspect_derived(derivative_class: str, derived_id: str, owner_id: str):
    adapter = CONTRACT_ADAPTERS.get(derivative_class)
    if adapter is None:
        raise HTTPException(status_code=404, detail="derivative class not found")
    try:
        object_id = UUID(derived_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="derived_id must be a UUID")

    row: dict[str, Any] | None
    if derivative_class == "derived_text":
        row = await pg.get_derived_text_for_owner(object_id, owner_id)
    elif derivative_class == "proactive_suggestion":
        row = await pg.get_proactive_suggestion(object_id)
        if row is not None and row.get("owner_id") != owner_id:
            row = None
    elif derivative_class == "memory_item":
        debug = await pg.get_memory_debug(object_id, owner_id)
        row = debug["memory"] if debug is not None else None
        if row is not None:
            row = {**row, "freshness_state": shape_memory_item(row)["freshness_state"]}
    else:
        debug = await pg.get_episode_debug(object_id, owner_id)
        row = debug["episode"] if debug is not None else None

    if row is None:
        raise HTTPException(status_code=404, detail="derived object not found")
    try:
        contract = adapter(row)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return DerivedInspectionResponse(
        derivative_class=derivative_class,
        contract=contract,
    )


@app.get(
    "/v1/internal/derived/{derivative_class}/{derived_id}/lifecycle",
    response_model=DerivedLifecycleInspection,
    tags=["derived-internal"],
    dependencies=[Depends(require_api_key)],
    summary="Inspect bounded lifecycle, rebuildability, source, and replay evidence for one derivative",
)
async def inspect_derived_lifecycle(derivative_class: str, derived_id: str, owner_id: str):
    if derivative_class not in DERIVED_CLASSES:
        raise HTTPException(status_code=404, detail="derivative class not found")
    try:
        object_id = UUID(derived_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="derived_id must be a UUID")
    row = await load_derived_row(pg, derived_class=derivative_class, derived_id=object_id, owner_id=owner_id)
    if row is None:
        raise HTTPException(status_code=404, detail="derived object not found")
    try:
        return DerivedLifecycleInspection(**inspect_lifecycle_row(derived_class=derivative_class, row=row))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.post(
    "/v1/internal/derived/{derivative_class}/{derived_id}/invalidate",
    response_model=DerivedInvalidationResponse,
    tags=["derived-internal"],
    dependencies=[Depends(require_api_key)],
    summary="Owner-scoped invalidation for one derived object with bounded audit evidence",
)
async def invalidate_derived_endpoint(derivative_class: str, derived_id: str, body: DerivedInvalidationRequest, request: Request):
    request_id = _require_matching_request_id(request, body.request_id)
    if derivative_class not in DERIVED_CLASSES:
        raise HTTPException(status_code=404, detail="derivative class not found")
    try:
        object_id = UUID(derived_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="derived_id must be a UUID")
    metadata = dict(body.metadata)
    if body.source_ref:
        metadata["source_ref_hash"] = structural_hash(body.source_ref)
    if body.derivation_version:
        metadata["derivation_version"] = body.derivation_version
    try:
        result = await invalidate_derived(
            pg,
            derived_class=derivative_class,
            derived_id=object_id,
            owner_id=body.owner_id,
            request_id=request_id,
            reason_code=body.reason_code,
            metadata=metadata,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if result is None:
        raise HTTPException(status_code=404, detail="derived object not found")
    return DerivedInvalidationResponse(
        request_id=request_id,
        changed=result["changed"],
        inspection=DerivedLifecycleInspection(**result["inspection"]),
    )


@app.post(
    "/v1/internal/derived/{derivative_class}/{derived_id}/replay",
    response_model=DerivedReplayResponse,
    tags=["derived-internal"],
    dependencies=[Depends(require_api_key)],
    summary="Deterministically replay or persist a rebuild for one derived object",
)
async def replay_derived_endpoint(derivative_class: str, derived_id: str, body: DerivedReplayRequest, request: Request):
    request_id = _require_matching_request_id(request, body.request_id)
    if derivative_class not in DERIVED_CLASSES:
        raise HTTPException(status_code=404, detail="derivative class not found")
    try:
        object_id = UUID(derived_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="derived_id must be a UUID")
    result = await replay_derived(
        pg,
        derived_class=derivative_class,
        derived_id=object_id,
        owner_id=body.owner_id,
        request_id=request_id,
        requested_derivation_version=body.requested_derivation_version,
        persist_replacement=body.persist_replacement,
        expected_current_derivation_version=body.expected_current_derivation_version,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="derived object not found")
    replay = result.get("replay") or {}
    inspection = {key: value for key, value in result.items() if key != "replay"}
    return DerivedReplayResponse(
        request_id=request_id,
        inspection=DerivedLifecycleInspection(**inspection),
        replay=replay,
    )


@app.post(
    "/v1/internal/recall/select",
    response_model=RecallSelectResponse,
    tags=["recall-internal"],
    summary="Deterministically select mentionability for explicitly supplied recall candidates",
)
async def select_recall(body: RecallSelectRequest, request: Request):
    request_id = _require_matching_request_id(request, body.request_id)
    context = body.context.model_dump(exclude_none=True)
    decisions = [
        select_recall_decision(
            context=context,
            candidate=candidate.model_dump(exclude_none=True),
        )
        for candidate in body.candidates
    ]
    rows = await pg.persist_recall_decisions(
        request_id=request_id,
        owner_id=body.owner_id,
        decisions=decisions,
    )
    shaped = [RecallDecisionItem(**shape_recall_decision(row)) for row in rows]
    return RecallSelectResponse(
        request_id=request_id,
        owner_id=body.owner_id,
        decision_count=len(shaped),
        decisions=shaped,
    )


@app.get(
    "/v1/internal/recall/debug/{request_id}",
    response_model=RecallDebugResponse,
    tags=["recall-internal"],
    summary="Inspect persisted recall selection decisions for one request",
)
async def debug_recall(request_id: str, owner_id: str):
    rows = await pg.get_recall_debug(request_id=request_id, owner_id=owner_id)
    if not rows:
        raise HTTPException(status_code=404, detail="recall decisions not found")
    shaped = [RecallDecisionItem(**shape_recall_decision(row)) for row in rows]
    return RecallDebugResponse(
        request_id=request_id,
        owner_id=owner_id,
        context=shaped[0].context,
        decision_count=len(shaped),
        decisions=shaped,
    )


@app.post(
    "/v1/profiles/resolve",
    response_model=ProfileResolveResponse,
    tags=["profiles"],
    dependencies=[Depends(require_api_key)],
    summary="Resolve effective profile for owner/surface/client",
)
async def resolve_profile(body: ProfileResolveRequest):
    if not settings.enable_profile_resolve:
        raise HTTPException(status_code=503, detail="profile resolve is disabled")
    out = await pg.resolve_profile(
        owner_id=body.owner_id,
        surface=body.surface,
        requested_profile=body.requested_profile,
        client_id=body.client_id,
        default_profile_name=settings.default_profile_name,
    )
    return ProfileResolveResponse(**out)


@app.post(
    "/v1/traces",
    response_model=TraceCreateResponse,
    tags=["traces"],
    dependencies=[Depends(require_api_key)],
    summary="Upsert one trace document per request",
)
async def create_trace(body: TraceCreateRequest, request: Request):
    _require_matching_request_id(request, body.request_id)
    if not settings.enable_trace_storage:
        raise HTTPException(status_code=503, detail="trace storage is disabled")

    try:
        conversation_id = UUID(body.conversation_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="conversation_id must be a UUID")

    trace_id = await pg.create_trace(
        {
            **body.model_dump(),
            "conversation_id": conversation_id,
        }
    )
    return TraceCreateResponse(trace_id=str(trace_id), request_id=body.request_id)


@app.get(
    "/v1/traces/{request_id}",
    response_model=TraceResponse,
    tags=["traces"],
    dependencies=[Depends(require_api_key)],
    summary="Get trace by request_id",
)
async def get_trace(request_id: str):
    trace = await pg.get_trace_by_request_id(request_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="trace not found")
    return TraceResponse(**trace)
