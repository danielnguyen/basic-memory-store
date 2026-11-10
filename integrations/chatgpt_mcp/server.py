"""Read-only MCP adapter for one owner's promoted BMS memories.

The tunnel is the only ingress. This adapter never accepts an owner, URL,
HTTP method, or BMS credential from its MCP caller.
"""

import os
from typing import Any
from uuid import UUID

import httpx
from mcp.server import MCPServer
from mcp.types import ToolAnnotations

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


if __name__ == "__main__":
    mcp.run(transport="stdio")
