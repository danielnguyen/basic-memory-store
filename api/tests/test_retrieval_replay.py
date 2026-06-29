import json
from pathlib import Path
import types
import uuid

import pytest

from models import ArtifactRef, ObservedMetadata, RetrievalBundle, RetrievalMessageItem, RetrievalOptions
from services.retrieval_replay import replay_raw_vs_augmented, structural_diff
from tools.replay_scenarios import run_corpus


async def fake_runner(*, settings, conversation_id, opts, include_artifacts=True, **kwargs):
    artifact_k = getattr(settings, "retrieval_artifact_k", 3) if include_artifacts else 0
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
        "contract_version": "raw-retrieval-debug.v1",
        "same_semantic_order": True,
        "raw_only_semantic_ids": [],
        "augmented_only_semantic_ids": [],
        "raw_order": ["message-1"],
        "augmented_order": ["message-1"],
        "added": [
            {
                "id": "artifact-1",
                "result_type": "artifact",
                "reason_codes": ["derivative_augmentation_used"],
            }
        ],
        "removed": [],
        "moved": [],
        "rank_deltas": [],
        "artifact_delta": 1,
        "token_delta": 20,
    }


@pytest.mark.asyncio
async def test_persisted_replay_corpus_matches_twice_from_clean_fixture_state():
    corpus = Path(__file__).resolve().parents[1] / "replay" / "retrieval_scenarios.v1.json"

    first, first_failures = await run_corpus(corpus)
    second, second_failures = await run_corpus(corpus)

    assert first_failures == []
    assert second_failures == []
    assert first == second
    assert len(first) == 13


def test_persisted_replay_corpus_covers_required_retrieval_matrix():
    corpus = Path(__file__).resolve().parents[1] / "replay" / "retrieval_scenarios.v1.json"
    payload = json.loads(corpus.read_text(encoding="utf-8"))
    categories = {
        category
        for scenario in payload["scenarios"]
        for category in scenario["categories"]
    }

    assert {
        "positive",
        "negative",
        "stale",
        "missing-source",
        "malformed",
        "dependency",
        "derived-store",
        "fallback",
        "offline",
        "local-routing",
        "cross-service",
        "truth-qualified",
        "parked",
        "contradicted",
        "superseded",
        "cross-owner",
        "source-traversal",
    } <= categories


def test_replay_snapshots_are_privacy_safe_and_structural_diff_is_readable():
    corpus = Path(__file__).resolve().parents[1] / "replay" / "retrieval_scenarios.v1.json"
    payload = corpus.read_text(encoding="utf-8")

    assert "neutral recent fixture" in payload
    expected_payloads = [scenario["expected"] for scenario in json.loads(payload)["scenarios"]]
    serialized_expected = json.dumps(expected_payloads)
    assert "neutral recent fixture" not in serialized_expected
    assert "stale derivative fixture" not in serialized_expected
    assert "parked derivative fixture" not in serialized_expected
    assert "contradicted derivative fixture" not in serialized_expected
    assert "cross owner derivative fixture" not in serialized_expected
    assert "source traversal unavailable fixture" not in serialized_expected
    diff = structural_diff({"ids": ["a"]}, {"ids": ["b"]})
    assert "--- expected" in diff
    assert "+++ actual" in diff
    assert '-    "a"' in diff
    assert '+    "b"' in diff
