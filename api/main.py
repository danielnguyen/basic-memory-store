from __future__ import annotations

import logging
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, UTC
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException, Security, Request, Response
from fastapi.security.api_key import APIKeyHeader
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest

from settings import get_settings
from clients.litellm import LiteLLMClient
from storage.postgres import PostgresStore
from storage.qdrant import QdrantStore, RetrievalHit as QdrantHit
from storage.object_store import ObjectStoreClient
from prompts.context import assemble_messages, build_context_block

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
    MessageCreateRequest,
    MessageCreateResponse,
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
    RetrievalBundle,
    RetrievalMessageItem,
    ObservedMetadata,
    ProfileResolveRequest,
    ProfileResolveResponse,
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


# -------------------------
# Artifacts
# -------------------------

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
            logging.exception("object store init failed", extra={"artifact_id": row["artifact_id"]})
            raise HTTPException(status_code=503, detail=f"artifact upload unavailable: {e}")

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

    if body.status == "completed" and settings.object_store_enabled:
        meta = object_store.head_object(existing["object_uri"])
        if meta is None:
            raise HTTPException(status_code=409, detail="artifact object is missing in object store")
        if int(meta.size) != int(existing["size"]):
            raise HTTPException(status_code=409, detail="artifact size mismatch with object store")

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
            logging.exception("object store download URL generation failed", extra={"artifact_id": row["artifact_id"]})
            raise HTTPException(status_code=503, detail=f"artifact download unavailable: {e}")

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
            logging.exception("object store download URL generation failed", extra={"artifact_id": row["artifact_id"]})
            raise HTTPException(status_code=503, detail=f"artifact download unavailable: {e}")

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

    if not await pg.conversation_exists(cid):
        raise HTTPException(status_code=404, detail="conversation_id not found")

    opts = body.retrieval or RetrievalOptions(k=settings.retrieval_k, min_score=0.25, scope="conversation")
    semantic_hits = await qdrant.search(
        owner_id=body.owner_id,
        query=body.query,
        k=opts.k,
        min_score=opts.min_score,
        conversation_id=cid,
        client_id=None,
    )
    semantic_ids = _safe_uuid_message_ids(
        semantic_hits,
        context="/v2/conversations/{id}/retrieve",
        kind="semantic",
    )
    semantic_snips = await pg.get_message_snippets_by_ids(semantic_ids)
    semantic_score_by_id = {h.message_id: h.score for h in semantic_hits}

    recent_snips = await pg.get_recent_message_items(conversation_id=cid, limit=settings.recent_turns)

    all_content = "".join([s["content"] for s in recent_snips] + [s["content"] for s in semantic_snips])
    has_code_like_content = any(tok in all_content for tok in ("```", "def ", "class ", "import ", "{", "};"))
    token_estimate_total = max(1, len(all_content) // 4) if all_content else None

    return RetrieveBundleResponse(
        request_id=body.request_id,
        conversation_id=str(cid),
        bundle=RetrievalBundle(
            recent=[
                RetrievalMessageItem(
                    message_id=s["message_id"],
                    conversation_id=s["conversation_id"],
                    role=s["role"],
                    content=s["content"],
                    created_at=s["created_at"],
                    score=None,
                )
                for s in recent_snips
            ],
            semantic=[
                RetrievalMessageItem(
                    message_id=s["message_id"],
                    conversation_id=s["conversation_id"],
                    role=s["role"],
                    content=s["content"],
                    created_at=s["created_at"],
                    score=semantic_score_by_id.get(s["message_id"]),
                )
                for s in semantic_snips
            ],
            artifact_refs=[],
            token_estimate_total=token_estimate_total,
            observed_metadata=ObservedMetadata(
                mime_types=[],
                has_artifacts=False,
                has_code_like_content=has_code_like_content,
                estimated_chars=len(all_content),
            ),
        ),
    )


# -------------------------
# Chat
# -------------------------

async def _run_chat(
    body: ChatRequest,
    request: Request,
    *,
    surface: str | None = None,
    artifact_ids: list[str] | None = None,
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
    recent = await pg.get_recent_messages(conversation_id=cid, limit=settings.recent_turns)

    system_preamble = (
        "You are a helpful assistant.\n"
        "- Use the provided context when relevant.\n"
        "- If context conflicts, prefer newer timestamps.\n"
        "- Do not invent facts.\n"
    )
    context_block = build_context_block(retrieved=retrieved, max_chars=settings.max_context_chars)
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
        retrieved_count=len(retrieved),
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
                    "profile": {},
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
                    "artifacts_used": artifact_ids or [],
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
        surface=body.surface,
        artifact_ids=body.artifact_ids or [],
    )
    return OrchestrateChatResponse(**resp.model_dump(), request_id=(getattr(request.state, "request_id", None) or ""))


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
