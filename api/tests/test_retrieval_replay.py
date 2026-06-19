import types
import uuid

import pytest

from models import ArtifactRef, ObservedMetadata, RetrievalBundle, RetrievalMessageItem, RetrievalOptions
from services.retrieval_replay import replay_raw_vs_augmented


async def fake_runner(*, settings, conversation_id, opts, **kwargs):
    artifact_k = getattr(settings, "retrieval_artifact_k", 3)
    artifacts = []
    if artifact_k:
        artifacts = [
            ArtifactRef(
                artifact_id="artifact-1",
                file_path="api/main.py",
                snippet="def entrypoint(): pass",
                relevance_score=0.8,
                repo_name="basic-memory-store",
                source_ref={"ref_type": "derived_text", "ref_id": "derived-text-fixture-1"},
            )
        ]
    return RetrievalBundle(
        recent=[],
        semantic=[
            RetrievalMessageItem(
                message_id="message-1",
                conversation_id=str(conversation_id),
                role="assistant",
                content="semantic note",
                created_at="2026-01-01T00:00:00+00:00",
                score=0.9,
                source_ref={"ref_type": "message", "ref_id": "message-1"},
            )
        ],
        artifact_refs=artifacts,
        token_estimate_total=10 + (20 if artifacts else 0),
        observed_metadata=ObservedMetadata(),
        retrieval_debug={"retrieval_mode": opts.retrieval_mode, "artifact_k": artifact_k},
    )


@pytest.mark.asyncio
async def test_replay_raw_vs_augmented_is_deterministic_and_fake_friendly():
    out = await replay_raw_vs_augmented(
        pg=object(),
        qdrant=object(),
        settings=types.SimpleNamespace(retrieval_artifact_k=3),
        owner_id="owner",
        conversation_id=uuid.uuid4(),
        client_id="client",
        query="hello",
        opts=RetrievalOptions(),
        runner=fake_runner,
    )

    assert out["raw"]["semantic_ids"] == ["message-1"]
    assert out["raw"]["artifact_count"] == 0
    assert out["augmented"]["artifact_ids"] == ["artifact-1"]
    assert out["comparison"] == {
        "same_semantic_order": True,
        "raw_only_semantic_ids": [],
        "augmented_only_semantic_ids": [],
        "artifact_delta": 1,
        "token_delta": 20,
    }
