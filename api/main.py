from __future__ import annotations

import logging
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.responses import JSONResponse
from fastapi.security.api_key import APIKeyHeader

from settings import get_settings
from clients.litellm import LiteLLMClient
from storage.postgres import PostgresStore
from storage.qdrant import QdrantStore, RetrievalHit as QdrantHit
from prompts.context import assemble_messages, build_context_block

from models import (
    ChatRequest,
    ChatResponse,
    ConversationCreateRequest,
    ConversationCreateResponse,
    ConversationListResponse,
    ConversationSummary,
    ConversationResolveRequest,
    ConversationResolveResponse,
    MessageCreateRequest,
    MessageCreateResponse,
    RetrieveRequest,
    RetrieveResponse,
    RetrieveHit,
    RetrievalOptions,
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
app = FastAPI(
    title="Basic Memory Store",
    version="0.1.0",
    swagger_ui_parameters={
        "persistAuthorization": True,
        "displayRequestDuration": True,
    },
    dependencies=[Depends(require_api_key)],
)


# --- Core clients/stores ---
pg = PostgresStore(settings.pg_dsn)
litellm = LiteLLMClient(settings.litellm_base_url, settings.litellm_api_key)
qdrant = QdrantStore(settings.qdrant_url, settings.qdrant_collection, litellm, settings.embed_model)


@app.on_event("startup")
async def startup() -> None:
    await pg.open()


@app.on_event("shutdown")
async def shutdown() -> None:
    await pg.close()


@app.get("/healthz", tags=["ops"], summary="Liveness probe")
async def healthz():
    return {"ok": True}


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



# -------------------------
# Conversations
# -------------------------

@app.post(
    "/v1/conversations",
    response_model=ConversationCreateResponse,
    tags=["conversations"],
    summary="Create a new conversation",
)
async def create_conversation(body: ConversationCreateRequest):
    cid = await pg.create_conversation(owner_id=body.owner_id, client_id=body.client_id, title=body.title)
    return ConversationCreateResponse(conversation_id=str(cid))

@app.get(
    "/v1/conversations",
    response_model=ConversationListResponse,
    tags=["conversations"],
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

    if body.role in ("user", "assistant"):
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
            logging.exception("qdrant upsert failed (non-fatal)", extra={"message_id": str(mid)})


    return MessageCreateResponse(message_id=str(mid))


# -------------------------
# Retrieval
# -------------------------

@app.post(
    "/v1/retrieve",
    response_model=RetrieveResponse,
    tags=["retrieve"],
    summary="Retrieve relevant past messages",
)
async def retrieve(body: RetrieveRequest):
    hits = await qdrant.search(
        owner_id=body.owner_id,
        query=body.query,
        k=body.k,
        min_score=body.min_score,
        conversation_id=body.conversation_id,
        client_id=body.client_id,
    )

    ids = [UUID(h.message_id) for h in hits]
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


# -------------------------
# Chat
# -------------------------

@app.post(
    "/v1/chat",
    response_model=ChatResponse,
    tags=["chat"],
    summary="Chat with retrieval-augmented memory",
)
async def chat(body: ChatRequest):
    owner_id = body.owner_id
    client_id = body.client_id

    created_new = False
    
    # Create conversation implicitly if not provided
    if not body.conversation_id:
        conversation_id = str(await pg.create_conversation(owner_id=owner_id, client_id=client_id, title=None))
        created_new = True
    else:
        conversation_id = body.conversation_id

    try:
        cid = UUID(conversation_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="conversation_id must be a UUID")

    if not created_new and  not await pg.conversation_exists(cid):
        raise HTTPException(status_code=404, detail="conversation_id not found")

    inserted_user_message_ids: set[str] = set()

    # Persist incoming user messages first
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
            logging.exception("qdrant upsert failed for user message (non-fatal)", extra={"message_id": str(mid)})


    if not last_user_text:
        raise HTTPException(status_code=400, detail="At least one user message is required.")

    # Retrieval (two-pass fallback)
    opts = body.retrieval or RetrievalOptions(k=settings.retrieval_k, min_score=0.25, scope="conversation")
    k = opts.k
    min_score = opts.min_score

    # If the client explicitly requested scope != conversation, respect it (no fallback).
    # Otherwise, do: conversation → fallback to owner if weak/empty.
    def _scope_filters(scope: str) -> tuple[str | None, str | None]:
        if scope == "conversation":
            return str(cid), None
        if scope == "client":
            return None, client_id
        # "owner"
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
        )

    # Pass 1
    retrieval_hits = []
    try:
        retrieval_hits = await _run_search(opts.scope, min_score)
    except Exception:
        logging.exception("qdrant search failed (non-fatal)")
        retrieval_hits = []


    # Fallback heuristic:
    # - Only when scope was "conversation"
    # - If zero hits, or fewer than half of k
    if opts.scope == "conversation" and (len(retrieval_hits) == 0 or len(retrieval_hits) < max(2, k // 2)):
        owner_min_score = min(1.0, min_score + 0.05)
        try:
            retrieval_hits = await _run_search("owner", owner_min_score)
        except Exception:
            logging.exception("qdrant owner-scope fallback search failed (non-fatal)")
            retrieval_hits = []


    # Drop self-matches (the message(s) we just inserted this request)
    filtered_hits = [h for h in retrieval_hits if h.message_id not in inserted_user_message_ids]

    retrieval_ids = [UUID(h.message_id) for h in filtered_hits]
    retrieved = await pg.get_message_snippets_by_ids(retrieval_ids)


    # Recent context window (conversation-local)
    recent = await pg.get_recent_messages(conversation_id=cid, limit=settings.recent_turns)

    # Prompt assembly
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

    # Call LiteLLM
    try:
        answer = await litellm.chat(model=settings.chat_model, messages=prompt_messages, temperature=0.2)
    except Exception as e:
        logging.exception("LiteLLM chat call failed")
        raise HTTPException(status_code=502, detail=str(e))

    # Persist assistant message + vector
    amid = await pg.add_message(
        conversation_id=cid,
        owner_id=owner_id,
        role="assistant",
        content=answer,
        client_id=client_id,
        metadata=None,
    )
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
        logging.exception("qdrant upsert failed for assistant message (non-fatal)", extra={"message_id": str(amid)})



    return JSONResponse(
        ChatResponse(
            conversation_id=str(cid),
            answer=answer,
            retrieved_count=len(retrieved),
        ).model_dump()
    )

