from __future__ import annotations

import asyncio
from types import SimpleNamespace
import uuid

import httpx

import main as main_module


class SmokePG:
    def __init__(self) -> None:
        self.conversation_id = str(uuid.uuid4())
        self.owner_id = "owner-smoke"
        self.client_id = "client-smoke"
        self.message_id = str(uuid.uuid4())
        self.derived_text_id = str(uuid.uuid4())
        self.artifact_id = str(uuid.uuid4())
        self.traces: dict[str, dict] = {}

    async def open(self):
        return None

    async def close(self):
        return None

    async def ping(self):
        return True

    async def get_conversation(self, cid):
        if str(cid) != self.conversation_id:
            return None
        return {
            "conversation_id": self.conversation_id,
            "owner_id": self.owner_id,
            "client_id": self.client_id,
            "title": None,
        }

    async def get_message_snippets_by_ids(self, ids):
        return [
            {
                "message_id": str(item),
                "conversation_id": self.conversation_id,
                "role": "assistant",
                "content": "private canonical smoke content",
                "metadata": {},
                "created_at": "2026-01-01T00:00:00+00:00",
            }
            for item in ids
        ]

    async def get_recent_message_items(self, *, conversation_id, limit):
        return [
            {
                "message_id": self.message_id,
                "conversation_id": str(conversation_id),
                "role": "user",
                "content": "private recent smoke content",
                "metadata": {},
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        ]

    async def get_message_owner(self, message_id):
        return self.owner_id

    async def get_derived_text_snippets_by_ids(self, ids):
        return [
            {
                "derived_text_id": str(item),
                "artifact_id": self.artifact_id,
                "owner_id": self.owner_id,
                "kind": "chunk",
                "text": "private derived smoke content",
                "derivation_params": {
                    "source_refs": [
                        {
                            "ref_type": "artifact",
                            "ref_id": self.artifact_id,
                            "support_kind": "direct",
                        }
                    ]
                },
                "created_at": "2026-01-01T00:00:00+00:00",
                "file_path": "smoke.txt",
                "repo_name": "basic-memory-store",
                "mime": "text/plain",
            }
            for item in ids
        ]

    async def get_artifact(self, artifact_id):
        return {"artifact_id": str(artifact_id), "owner_id": self.owner_id}

    async def get_event_ingest_log(self, event_log_id):
        return None

    async def get_memory_debug(self, memory_id, owner_id):
        return None

    async def get_derived_text_for_owner(self, *, derived_text_id, owner_id):
        return None

    async def get_memory_items_for_source_refs(self, *, owner_id, source_refs):
        return {}

    async def create_trace(self, trace):
        trace_id = str(uuid.uuid4())
        self.traces[trace["request_id"]] = {
            "trace_id": trace_id,
            **trace,
            "conversation_id": str(trace["conversation_id"]),
            "created_at": "2026-01-01T00:00:00+00:00",
        }
        return trace_id

    async def get_trace_by_request_id(self, request_id):
        return self.traces.get(request_id)


class SmokeQdrant:
    def __init__(self, pg: SmokePG) -> None:
        self.pg = pg
        self.artifact_calls = 0

    def ping(self):
        return True

    async def search(self, **kwargs):
        return [SimpleNamespace(message_id=self.pg.message_id, score=0.8)]

    async def search_artifact_chunks(self, **kwargs):
        self.artifact_calls += 1
        return [SimpleNamespace(derived_text_id=self.pg.derived_text_id, score=0.7)]


async def _post(client: httpx.AsyncClient, pg: SmokePG, request_id: str, mode: str):
    query = " PRIVATE-SMOKE-QUERY " if mode == "compare" else "smoke"
    return await client.post(
        f"/v2/conversations/{pg.conversation_id}/retrieve",
        headers={"X-API-Key": "testkey", "X-Request-ID": request_id},
        json={
            "request_id": request_id,
            "owner_id": pg.owner_id,
            "query": query,
            "mode": mode,
        },
    )


async def main() -> None:
    pg = SmokePG()
    qdrant = SmokeQdrant(pg)
    main_module.pg = pg
    main_module.qdrant = qdrant
    main_module.settings = SimpleNamespace(
        memory_api_key="testkey",
        require_request_id=True,
        enforce_request_id_header_body_match=True,
        retrieval_k=8,
        retrieval_recent_half_life_days=14,
        retrieval_balanced_half_life_days=45,
        retrieval_historical_half_life_days=365,
        retrieval_conversation_boost=0.08,
        retrieval_pinned_bias=0.12,
        retrieval_missing_penalty_cap=0.15,
        recent_turns=10,
        enable_trace_storage=True,
    )
    async with main_module.app.router.lifespan_context(main_module.app):
        transport = httpx.ASGITransport(app=main_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://smoke") as client:
            raw = await _post(client, pg, "smoke-raw", "raw")
            assert raw.status_code == 200, raw.text
            raw_body = raw.json()
            assert raw_body["bundle"]["artifact_refs"] == []
            assert raw_body["diagnostics"]["mode"] == "raw"

            compare = await _post(client, pg, "smoke-compare", "compare")
            assert compare.status_code == 200, compare.text
            compare_body = compare.json()
            assert compare_body["raw_bundle"]["artifact_refs"] == []
            assert len(compare_body["augmented_bundle"]["artifact_refs"]) == 1
            assert compare_body["comparison"]["artifact_delta"] == 1
            assert compare_body["comparison"]["shared_normalized_input"] is True
            assert compare_body["comparison"]["normalization_applied"] is True
            assert "PRIVATE-SMOKE-QUERY" not in str(compare_body["comparison"])
            assert "private canonical smoke content" not in str(compare_body["comparison"])
            assert "private derived smoke content" not in str(compare_body["diagnostics"])

            trace = await client.get(
                "/v1/traces/smoke-compare",
                headers={"X-API-Key": "testkey"},
            )
            assert trace.status_code == 200, trace.text
            trace_body = trace.json()
            assert trace_body["retrieval"]["mode"] == "compare"
            assert trace_body["retrieval"]["request_id"] == "smoke-compare"
            assert "PRIVATE-SMOKE-QUERY" not in str(trace_body["retrieval"])

            unauthorized = await client.post(
                f"/v2/conversations/{pg.conversation_id}/retrieve",
                headers={"X-Request-ID": "smoke-unauth"},
                json={
                    "request_id": "smoke-unauth",
                    "owner_id": pg.owner_id,
                    "query": "smoke",
                    "mode": "raw",
                },
            )
            assert unauthorized.status_code == 401

    print("raw retrieval smoke passed: raw, compare, trace, unauthorized")


if __name__ == "__main__":
    asyncio.run(main())
