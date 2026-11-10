"""Read-only MCP adapter for one owner's promoted BMS memories.

The tunnel is the only ingress. This adapter never accepts an owner, URL,
HTTP method, or BMS credential from its MCP caller.
"""

import os
from datetime import datetime, timezone
from typing import Annotated, Any, Literal
from uuid import UUID

import httpx
from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field, ValidationError

mcp = MCPServer("BMS memory (read only)")


async def read_memory(memory_id: str) -> dict[str, Any]:
    try:
        canonical_id = str(UUID(memory_id))
    except (ValueError, TypeError, AttributeError):
        return {"found": False, "reason": "invalid_id"}

    owner = os.environ.get("BMS_OWNER_ID", "").strip()
    key = os.environ.get("MEMORY_API_KEY", "").strip()
    base = os.environ.get("BMS_API_URL", "http://basic-memory-store:8000").rstrip("/")
    if not owner or not key:
        return {"found": False, "reason": "not_configured"}

    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
            response = await client.get(
                f"{base}/v1/internal/memory/{canonical_id}/debug",
                params={"owner_id": owner},
                headers={"X-API-Key": key, "Accept": "application/json"},
            )
        if response.status_code == 404:
            return {"found": False, "reason": "not_found"}
        if response.status_code != 200:
            return {"found": False, "reason": "bms_unavailable"}
        value = response.json()
    except (httpx.HTTPError, ValueError):
        return {"found": False, "reason": "bms_unavailable"}

    memory = value.get("memory") if isinstance(value, dict) else None
    if not isinstance(memory, dict) or memory.get("owner_id") != owner or memory.get("memory_id") != canonical_id:
        return {"found": False, "reason": "not_found"}

    return {
        "found": True,
        "memory_id": memory["memory_id"],
        "summary": memory.get("summary"),
        "memory_type": memory.get("memory_type"),
        "status": memory.get("status"),
        "promotion_state": memory.get("promotion_state"),
        "source_refs": memory.get("source_refs") if isinstance(memory.get("source_refs"), list) else [],
        "expires_at": memory.get("expires_at"),
        "updated_at": memory.get("updated_at"),
    }


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
async def get_memory(memory_id: str) -> dict[str, Any]:
    """Retrieve a promoted BMS memory by its UUID for the configured owner.

    Only reports BMS records actually returned. A not-found result is not a
    remembered fact. No writes or arbitrary API requests are possible.
    """
    return await read_memory(memory_id)


class SearchInput(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)
    limit: int = Field(5, ge=1, le=10, strict=True)


class SearchSourceRef(BaseModel):
    ref_type: str = Field(..., min_length=1, max_length=64)
    ref_id: str = Field(..., min_length=1, max_length=160)
    support_kind: str = Field(default="direct", min_length=1, max_length=64)


class SearchMemory(BaseModel):
    memory_id: UUID
    owner_id: str = Field(..., min_length=1, max_length=160)
    summary: str = Field(..., min_length=1, max_length=4000)
    memory_type: str = Field(..., min_length=1, max_length=64)
    source_refs: list[SearchSourceRef] = Field(..., max_length=50)
    confidence: float | None = Field(default=None, ge=0, le=1)
    status: Literal["active"]
    promotion_state: Literal["promoted"]
    freshness_state: Literal["active"]
    expires_at: datetime | None = None
    updated_at: datetime


class SearchResponse(BaseModel):
    memories: list[SearchMemory] = Field(..., max_length=10)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
async def search_memories(
    query: Annotated[str, Field(min_length=1, max_length=500)],
    limit: Annotated[int, Field(ge=1, le=10, strict=True)] = 5,
) -> dict[str, Any]:
    """Find current promoted memories by words or a short English question.

    Searches only the configured owner's stored summaries. Use returned sources
    as evidence; an empty successful result means no matching memory was found.
    A failure is not evidence that no memory exists. No writes are exposed.
    """
    try:
        inputs = SearchInput(query=query, limit=limit)
        if not inputs.query.strip():
            return {"ok": False, "reason": "invalid_query"}
    except ValidationError:
        return {"ok": False, "reason": "invalid_query"}

    owner = os.environ.get("BMS_OWNER_ID", "").strip()
    key = os.environ.get("MEMORY_API_KEY", "").strip()
    base = os.environ.get("BMS_API_URL", "http://basic-memory-store:8000").rstrip("/")
    if not owner or not key:
        return {"ok": False, "reason": "not_configured"}

    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
            response = await client.get(
                f"{base}/v1/internal/memory/search",
                params={"owner_id": owner, "query": inputs.query, "limit": inputs.limit},
                headers={"X-API-Key": key, "Accept": "application/json"},
            )
        if response.status_code == 422:
            return {"ok": False, "reason": "invalid_query"}
        if response.status_code != 200:
            return {"ok": False, "reason": "bms_unavailable"}
        result = SearchResponse.model_validate(response.json())
        if len(result.memories) > inputs.limit:
            return {"ok": False, "reason": "bms_unavailable"}
        now = datetime.now(timezone.utc)
        for memory in result.memories:
            if (memory.owner_id != owner or memory.updated_at.tzinfo is None
                    or (memory.expires_at is not None and (
                        memory.expires_at.tzinfo is None or memory.expires_at <= now))):
                return {"ok": False, "reason": "bms_unavailable"}
    except (httpx.HTTPError, ValueError):
        return {"ok": False, "reason": "bms_unavailable"}
    return {"ok": True, "memories": [
        memory.model_dump(mode="json", exclude={"owner_id"}) for memory in result.memories
    ]}


if __name__ == "__main__":
    mcp.run(transport="stdio")
