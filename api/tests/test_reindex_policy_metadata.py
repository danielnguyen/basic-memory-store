from __future__ import annotations

import sys
import types
import uuid

import pytest

from storage.qdrant import _policy_payload
from tools import reindex


@pytest.mark.asyncio
async def test_reindex_uses_dedicated_policy_metadata_only(monkeypatch):
    legacy_spoof_id = uuid.uuid4()
    malformed_id = uuid.uuid4()
    structured_id = uuid.uuid4()
    valid_policy = {
        "memory_domains": ["technical"],
        "sensitivity": "low",
        "entity_ids": [],
        "relationship_ids": [],
        "relationship_scopes": [],
    }
    rows = [
        {
            "message_id": legacy_spoof_id,
            "conversation_id": uuid.uuid4(),
            "owner_id": "owner",
            "client_id": "client",
            "role": "assistant",
            "content": "legacy spoof",
            "metadata": {"retrieval_policy_metadata": valid_policy},
            "policy_metadata": None,
        },
        {
            "message_id": malformed_id,
            "conversation_id": uuid.uuid4(),
            "owner_id": "owner",
            "client_id": "client",
            "role": "assistant",
            "content": "malformed policy",
            "metadata": {},
            "policy_metadata": {"memory_domains": "", "sensitivity": "low"},
        },
        {
            "message_id": structured_id,
            "conversation_id": uuid.uuid4(),
            "owner_id": "owner",
            "client_id": "client",
            "role": "assistant",
            "content": "structured policy",
            "metadata": {},
            "policy_metadata": valid_policy,
        },
    ]
    recorded_payloads = {}

    class FakePG:
        def __init__(self, dsn):
            self.calls = 0

        async def open(self):
            return None

        async def close(self):
            return None

        async def get_messages_for_reindex(self, **kwargs):
            self.calls += 1
            return rows if self.calls == 1 else []

    class FakeQdrant:
        def __init__(self, *args, **kwargs):
            return None

        async def upsert_message_vector(self, **kwargs):
            recorded_payloads[str(kwargs["message_id"])] = _policy_payload(kwargs.get("policy_metadata"))

    class FakeLiteLLM:
        def __init__(self, *args, **kwargs):
            return None

    monkeypatch.setattr(reindex, "PostgresStore", FakePG)
    monkeypatch.setattr(reindex, "QdrantStore", FakeQdrant)
    monkeypatch.setattr(reindex, "LiteLLMClient", FakeLiteLLM)
    monkeypatch.setattr(
        reindex,
        "get_settings",
        lambda: types.SimpleNamespace(
            pg_dsn="",
            litellm_base_url="",
            litellm_api_key=None,
            qdrant_url="",
            qdrant_collection="messages",
            embed_model="embed",
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["reindex", "--owner-id", "owner", "--batch-size", "3", "--max", "3"],
    )

    await reindex.main()

    assert recorded_payloads[str(legacy_spoof_id)] == {"retrieval_policy_valid": False}
    assert recorded_payloads[str(malformed_id)] == {"retrieval_policy_valid": False}
    assert recorded_payloads[str(structured_id)]["retrieval_policy_valid"] is True
    assert recorded_payloads[str(structured_id)]["memory_domains"] == ["technical"]
