from __future__ import annotations

import types
from uuid import uuid4

from fastapi.testclient import TestClient
import psycopg

import main as main_module
from storage.postgres import PostgresStore


class FakeQdrant:
    def __init__(self):
        self.derived_text_ids: list[str] = []
        self.vector_unavailable = False
        self.artifact_unavailable = False

    def ping(self):
        return True

    async def upsert_derived_text_vector(self, *, derived_text_id, **kwargs):
        self.derived_text_ids.append(str(derived_text_id))

    async def upsert_message_vector(self, **kwargs):
        return None

    async def search(self, **kwargs):
        if self.vector_unavailable:
            raise RuntimeError("vector unavailable")
        return []

    async def search_artifact_chunks(self, **kwargs):
        if self.artifact_unavailable:
            raise RuntimeError("artifact unavailable")
        return [
            types.SimpleNamespace(
                derived_text_id=derived_id,
                artifact_id="unused",
                file_path="proof.txt",
                repo_name="fixture",
                score=0.91,
            )
            for derived_id in self.derived_text_ids
        ]


def _settings():
    return types.SimpleNamespace(
        memory_api_key="testkey",
        require_request_id=True,
        enforce_request_id_header_body_match=True,
        default_profile_name="dev",
        retrieval_k=8,
        retrieval_recent_half_life_days=14,
        retrieval_balanced_half_life_days=45,
        retrieval_historical_half_life_days=365,
        retrieval_conversation_boost=0.08,
        retrieval_pinned_bias=0.12,
        retrieval_missing_penalty_cap=0.15,
        retrieval_artifact_k=3,
        retrieval_artifact_max_snippet_chars=500,
        recent_turns=10,
        ingest_allowed_extensions=".txt",
        ingest_exclude_globs_default=".git,node_modules",
        ingest_max_files_per_request=10,
        ingest_max_file_bytes=100_000,
        ingest_chunk_size_chars=1_000,
        ingest_chunk_overlap_chars=0,
        embed_model="test-embed",
        min_index_chars=3,
        index_assistant_messages=True,
        index_user_questions=True,
    )


def _headers(request_id: str | None = None) -> dict[str, str]:
    headers = {"X-API-Key": "testkey"}
    if request_id is not None:
        headers["X-Request-ID"] = request_id
    return headers


def _inspect(client: TestClient, derivative_class: str, derived_id: str, owner_id: str = "owner-a"):
    return client.get(
        f"/v1/internal/derived/{derivative_class}/{derived_id}",
        headers=_headers(),
        params={"owner_id": owner_id},
    )


def test_real_creation_paths_storage_reopen_retrieval_and_owner_isolation(
    monkeypatch,
    postgres_database,
    tmp_path,
):
    qdrant = FakeQdrant()
    first_store = PostgresStore(postgres_database)
    monkeypatch.setattr(main_module, "settings", _settings(), raising=True)
    monkeypatch.setattr(main_module, "pg", first_store, raising=True)
    monkeypatch.setattr(main_module, "qdrant", qdrant, raising=True)

    source_file = tmp_path / "proof.txt"
    source_file.write_text("Neutral provenance fixture.", encoding="utf-8")

    with TestClient(main_module.app) as client:
        conversation = client.post(
            "/v1/conversations",
            headers=_headers(),
            json={"owner_id": "owner-a", "client_id": "test", "title": "provenance proof"},
        )
        assert conversation.status_code == 200, conversation.text
        conversation_id = conversation.json()["conversation_id"]
        canonical_message = client.post(
            f"/v1/conversations/{conversation_id}/messages",
            headers=_headers(),
            json={
                "owner_id": "owner-a",
                "client_id": "test",
                "role": "user",
                "content": "Neutral canonical retrieval proof.",
            },
        )
        assert canonical_message.status_code == 200, canonical_message.text

        ingested = client.post(
            "/v1/ingestion/files",
            headers=_headers(),
            json={"owner_id": "owner-a", "repo_name": "fixture", "paths": [str(source_file)]},
        )
        assert ingested.status_code == 200, ingested.text
        assert ingested.json()["chunks_created"] == 1
        derived_text_id = qdrant.derived_text_ids[0]
        foreign_file = tmp_path / "foreign.txt"
        foreign_file.write_text("Foreign owner fixture.", encoding="utf-8")
        foreign_ingested = client.post(
            "/v1/ingestion/files",
            headers=_headers(),
            json={"owner_id": "owner-b", "repo_name": "fixture", "paths": [str(foreign_file)]},
        )
        assert foreign_ingested.status_code == 200

        event = client.post(
            "/v1/events/ingest",
            headers=_headers("rid-event"),
            json={
                "request_id": "rid-event",
                "owner_id": "owner-a",
                "source_type": "portfolio",
                "source_event_id": "portfolio-proof-1",
                "event_type": "allocation_drift",
                "payload_json": {"account": "test account", "allocation_drift_pct": 0.09},
            },
        )
        assert event.status_code == 200, event.text
        event_body = event.json()
        event_log_id = event_body["event_log_id"]
        event_message_id = event_body["message_id"]
        prefs = client.put(
            "/v1/proactive/preferences",
            headers=_headers(),
            json={
                "owner_id": "owner-a",
                "enabled": True,
                "allowed_surfaces_json": ["telegram"],
                "rule_prefs_json": {"portfolio": {"drift_threshold": 0.05}},
            },
        )
        assert prefs.status_code == 200
        evaluated = client.post(
            "/v1/internal/proactive/evaluate",
            headers=_headers("rid-proactive"),
            json={
                "request_id": "rid-proactive",
                "owner_id": "owner-a",
                "event_log_id": event_log_id,
            },
        )
        assert evaluated.status_code == 200, evaluated.text
        proactive_id = evaluated.json()["suggestions"][0]["suggestion_id"]

        promoted = client.post(
            "/v1/internal/memory/promote",
            headers=_headers("rid-memory"),
            json={
                "request_id": "rid-memory",
                "owner_id": "owner-a",
                "memory_type": "preference",
                "summary": "Use bounded neutral fixtures.",
                "source_refs": [{"ref_type": "event_log", "ref_id": event_log_id}],
                "confidence": 0.8,
                "explanation": {"rationale": "explicit fixture evidence"},
                "generation_trace_id": "trace-memory",
            },
        )
        assert promoted.status_code == 200, promoted.text
        memory = promoted.json()["memory"]
        derived_memory_response = client.post(
            "/v1/internal/memory/promote",
            headers=_headers("rid-derived-memory"),
            json={
                "request_id": "rid-derived-memory",
                "owner_id": "owner-a",
                "memory_type": "derived_text_note",
                "summary": "Derived artifact qualification record.",
                "source_refs": [{"ref_type": "derived_text", "ref_id": derived_text_id}],
                "confidence": 0.64,
                "explanation": {"rationale": "retrieval qualification fixture"},
                "generation_trace_id": "trace-derived-memory",
            },
        )
        assert derived_memory_response.status_code == 200, derived_memory_response.text
        derived_memory_id = derived_memory_response.json()["memory"]["memory_id"]
        derived_transition = client.post(
            f"/v1/internal/memory/{derived_memory_id}/transition",
            headers=_headers("rid-derived-stale"),
            json={
                "request_id": "rid-derived-stale",
                "owner_id": "owner-a",
                "status": "stale",
                "reason": {"code": "source_recheck_required"},
            },
        )
        assert derived_transition.status_code == 200, derived_transition.text
        replacement_response = client.post(
            "/v1/internal/memory/promote",
            headers=_headers("rid-memory-replacement"),
            json={
                "request_id": "rid-memory-replacement",
                "owner_id": "owner-a",
                "memory_type": "preference",
                "summary": "Use the corrected bounded fixture.",
                "source_refs": [{"ref_type": "message", "ref_id": event_message_id}],
                "supersedes_memory_id": memory["memory_id"],
            },
        )
        assert replacement_response.status_code == 200, replacement_response.text
        replacement = replacement_response.json()["memory"]
        original_after_supersession = _inspect(client, "memory_item", memory["memory_id"])
        assert original_after_supersession.status_code == 200
        assert original_after_supersession.json()["contract"]["source_refs"] == memory["source_refs"]
        assert original_after_supersession.json()["contract"]["status"] == "superseded"

        episode_created = client.post(
            "/v1/internal/episodes",
            headers=_headers("rid-episode"),
            json={
                "request_id": "rid-episode",
                "owner_id": "owner-a",
                "title": "Provenance proof",
                "summary": "Created through the production episode endpoint.",
                "episode_type": "milestone",
                "source_refs": [{"ref_type": "memory_item", "ref_id": replacement["memory_id"]}],
                "confidence": 0.7,
                "explanation": {"rationale": "bounded integration evidence"},
                "generation_trace_id": "trace-episode",
            },
        )
        assert episode_created.status_code == 200, episode_created.text
        episode_id = episode_created.json()["episode"]["episode_id"]

        inspections = {
            "derived_text": _inspect(client, "derived_text", derived_text_id),
            "proactive_suggestion": _inspect(client, "proactive_suggestion", proactive_id),
            "memory_item": _inspect(client, "memory_item", memory["memory_id"]),
            "episode": _inspect(client, "episode", episode_id),
        }
        assert all(response.status_code == 200 for response in inspections.values())
        contracts = {name: response.json()["contract"] for name, response in inspections.items()}
        common_fields = set(contracts["derived_text"])
        assert all(set(contract) == common_fields for contract in contracts.values())
        assert all(contract["owner_id"] == "owner-a" for contract in contracts.values())
        assert all(contract["source_refs"] for contract in contracts.values())
        assert all(contract["derivation_version"] for contract in contracts.values())
        assert all(contract["status"] for contract in contracts.values())
        assert contracts["derived_text"]["source_refs"][0]["ref_type"] == "artifact"
        assert contracts["proactive_suggestion"]["source_refs"] == [
            {"ref_type": "event_log", "ref_id": event_log_id, "support_kind": "direct"}
        ]
        assert contracts["proactive_suggestion"]["generation_trace_id"] == "rid-proactive"
        assert contracts["memory_item"]["confidence"] == 0.8
        assert contracts["memory_item"]["generation_trace_id"] == "trace-memory"
        assert contracts["episode"]["confidence"] == 0.7
        assert contracts["episode"]["generation_trace_id"] == "trace-episode"

        retrieval = client.post(
            f"/v2/conversations/{conversation_id}/retrieve",
            headers=_headers("rid-retrieve"),
            json={
                "request_id": "rid-retrieve",
                "owner_id": "owner-a",
                "query": "provenance",
                "include_artifacts": True,
            },
        )
        assert retrieval.status_code == 200, retrieval.text
        artifact = retrieval.json()["bundle"]["artifact_refs"][0]
        assert len(retrieval.json()["bundle"]["artifact_refs"]) == 1
        assert retrieval.json()["bundle"]["recent"][0]["evidence_role"] == "canonical"
        assert retrieval.json()["bundle"]["recent"][0]["source_availability"] == "not_applicable"
        assert retrieval.json()["bundle"]["retrieval_debug"]["cross_owner_artifact_provenance_count"] == 1
        truth = retrieval.json()["bundle"]["retrieval_debug"]["truth_qualification"]
        assert truth["canonical_result_count"] >= 1
        assert truth["derived_result_count"] == 1
        assert truth["source_available_count"] == 1
        assert truth["source_owner_mismatch_count"] == 1
        assert truth["lifecycle_restricted_derived_count"] == 1
        assert artifact["source_ref"] == {"ref_type": "derived_text", "ref_id": derived_text_id}
        assert artifact["evidence_role"] == "derived"
        assert artifact["source_availability"] == "available"
        assert artifact["source_checks"] == [
            {
                "ref_type": "artifact",
                "ref_id": artifact["provenance"]["source_refs"][0]["ref_id"],
                "support_kind": "direct",
                "availability": "available",
            }
        ]
        assert artifact["memory_id"] == derived_memory_id
        assert artifact["durable_status"] == "stale"
        assert artifact["freshness_state"] == "stale"
        assert artifact["confidence"] == 0.64
        assert artifact["qualification_reasons"] == ["effective_stale"]
        assert artifact["provenance"] == {
            **contracts["derived_text"],
            "retrieval_reason": "included_by_artifact_similarity",
        }
        assert "text" not in artifact["provenance"]
        assert "derivation_params" not in artifact["provenance"]

        qdrant.artifact_unavailable = True
        degraded_retrieval = client.post(
            f"/v2/conversations/{conversation_id}/retrieve",
            headers=_headers("rid-artifact-unavailable"),
            json={
                "request_id": "rid-artifact-unavailable",
                "owner_id": "owner-a",
                "query": "provenance",
                "include_artifacts": True,
            },
        )
        assert degraded_retrieval.status_code == 200, degraded_retrieval.text
        degraded_body = degraded_retrieval.json()["bundle"]
        assert degraded_body["recent"][0]["evidence_role"] == "canonical"
        assert degraded_body["artifact_refs"] == []
        assert degraded_body["retrieval_debug"]["artifact_status"] == "unavailable"
        qdrant.artifact_unavailable = False

        assert _inspect(client, "memory_item", memory["memory_id"], "owner-b").status_code == 404
        cross_owner_retrieval = client.post(
            f"/v2/conversations/{conversation_id}/retrieve",
            headers=_headers("rid-cross-owner"),
            json={
                "request_id": "rid-cross-owner",
                "owner_id": "owner-b",
                "query": "provenance",
            },
        )
        assert cross_owner_retrieval.status_code == 404

        malformed = client.post(
            "/v1/internal/memory/promote",
            headers=_headers("rid-malformed"),
            json={
                "request_id": "rid-malformed",
                "owner_id": "owner-a",
                "memory_type": "preference",
                "summary": "Malformed source ref",
                "source_refs": [{"ref_type": " ", "ref_id": "source"}],
            },
        )
        assert malformed.status_code == 422

        lifecycle = client.get(
            f"/v1/internal/derived/derived_text/{derived_text_id}/lifecycle",
            headers=_headers(),
            params={"owner_id": "owner-a"},
        )
        assert lifecycle.status_code == 200, lifecycle.text
        assert lifecycle.json()["rebuildability"] == "rebuildable"
        snapshot = lifecycle.json()["structural_snapshot"]
        assert snapshot["status"] == "active"

        identical_replay = client.post(
            f"/v1/internal/derived/derived_text/{derived_text_id}/replay",
            headers=_headers("rid-derived-identical"),
            json={
                "request_id": "rid-derived-identical",
                "owner_id": "owner-a",
                "expected_current_derivation_version": "file-chunk-v1",
                "persist_replacement": False,
            },
        )
        assert identical_replay.status_code == 200, identical_replay.text
        assert identical_replay.json()["replay"]["result"] == "identical"

        unsupported_version = client.post(
            f"/v1/internal/derived/derived_text/{derived_text_id}/replay",
            headers=_headers("rid-derived-unsupported-version"),
            json={
                "request_id": "rid-derived-unsupported-version",
                "owner_id": "owner-a",
                "requested_derivation_version": "file-chunk-v999",
                "persist_replacement": True,
            },
        )
        assert unsupported_version.status_code == 200, unsupported_version.text
        assert unsupported_version.json()["replay"]["result"] == "unsupported"
        assert unsupported_version.json()["replay"]["failure_reason"] == "unsupported_derivation_version"

        invalidated = client.post(
            f"/v1/internal/derived/derived_text/{derived_text_id}/invalidate",
            headers=_headers("rid-derived-invalidate"),
            json={
                "request_id": "rid-derived-invalidate",
                "owner_id": "owner-a",
                "reason_code": "source_changed",
                "metadata": {"private_text": "x" * 500, "nested": {"drop": True}},
            },
        )
        assert invalidated.status_code == 200, invalidated.text
        assert invalidated.json()["changed"] is True
        repeated_invalidated = client.post(
            f"/v1/internal/derived/derived_text/{derived_text_id}/invalidate",
            headers=_headers("rid-derived-invalidate"),
            json={
                "request_id": "rid-derived-invalidate",
                "owner_id": "owner-a",
                "reason_code": "source_changed",
            },
        )
        assert repeated_invalidated.status_code == 200
        assert repeated_invalidated.json()["changed"] is False

        source_file.write_text("Neutral provenance fixture changed for deterministic replay.", encoding="utf-8")
        replaced = client.post(
            f"/v1/internal/derived/derived_text/{derived_text_id}/replay",
            headers=_headers("rid-derived-replace"),
            json={
                "request_id": "rid-derived-replace",
                "owner_id": "owner-a",
                "expected_current_derivation_version": "file-chunk-v1",
                "persist_replacement": True,
            },
        )
        assert replaced.status_code == 200, replaced.text
        assert replaced.json()["replay"]["result"] == "replaced"
        replacement_id = replaced.json()["replay"]["replacement_id"]
        assert replacement_id
        predecessor_lifecycle = client.get(
            f"/v1/internal/derived/derived_text/{derived_text_id}/lifecycle",
            headers=_headers(),
            params={"owner_id": "owner-a"},
        )
        assert predecessor_lifecycle.status_code == 200
        assert predecessor_lifecycle.json()["contract"]["status"] == "superseded"
        assert predecessor_lifecycle.json()["lifecycle_status"] == "superseded"
        assert predecessor_lifecycle.json()["events"][-1]["result"] == "replaced"
        replacement_inspection = _inspect(client, "derived_text", replacement_id)
        assert replacement_inspection.status_code == 200
        assert replacement_inspection.json()["contract"]["source_refs"] == contracts["derived_text"]["source_refs"]

    second_store = PostgresStore(postgres_database)
    monkeypatch.setattr(main_module, "pg", second_store, raising=True)
    with TestClient(main_module.app) as reopened_client:
        for derivative_class, derived_id in (
            ("derived_text", derived_text_id),
            ("proactive_suggestion", proactive_id),
            ("memory_item", memory["memory_id"]),
            ("episode", episode_id),
        ):
            persisted = _inspect(reopened_client, derivative_class, derived_id)
            assert persisted.status_code == 200, persisted.text
            assert persisted.json()["contract"]["source_refs"]
        reopened_lifecycle = reopened_client.get(
            f"/v1/internal/derived/derived_text/{derived_text_id}/lifecycle",
            headers=_headers(),
            params={"owner_id": "owner-a"},
        )
        assert reopened_lifecycle.status_code == 200
        assert reopened_lifecycle.json()["contract"]["status"] == "superseded"
        assert any(event.get("result") == "replaced" for event in reopened_lifecycle.json()["events"])


def test_legacy_defaults_and_malformed_stored_provenance_are_explicit(
    monkeypatch,
    postgres_database,
):
    artifact_id = uuid4()
    legacy_id = uuid4()
    malformed_id = uuid4()
    with psycopg.connect(postgres_database) as conn:
        conn.execute(
            """
            INSERT INTO artifacts (
                id, owner_id, mime, size, object_uri, filename, status
            ) VALUES (%s, 'owner-a', 'text/plain', 1, 'file:///legacy', 'legacy.txt', 'completed')
            """,
            (artifact_id,),
        )
        conn.execute(
            """
            INSERT INTO derived_text (id, artifact_id, kind, text, derivation_params)
            VALUES
              (%s, %s, 'chunk', 'legacy', %s::jsonb),
              (%s, %s, 'chunk', 'malformed', %s::jsonb)
            """,
            (
                legacy_id,
                artifact_id,
                '{"source_refs":[{"ref_type":"artifact","ref_id":"legacy","support_kind":"direct"}]}',
                malformed_id,
                artifact_id,
                '{"source_refs":[{"ref_type":"artifact","ref_id":"","support_kind":"direct"}]}',
            ),
        )
        conn.commit()

    monkeypatch.setattr(main_module, "settings", _settings(), raising=True)
    monkeypatch.setattr(main_module, "pg", PostgresStore(postgres_database), raising=True)
    qdrant = FakeQdrant()
    qdrant.derived_text_ids.append(str(malformed_id))
    monkeypatch.setattr(main_module, "qdrant", qdrant, raising=True)
    with TestClient(main_module.app) as client:
        legacy = _inspect(client, "derived_text", str(legacy_id))
        assert legacy.status_code == 200
        assert legacy.json()["contract"]["compatibility_defaults"] == [
            "derivation_version",
            "status",
        ]
        malformed = _inspect(client, "derived_text", str(malformed_id))
        assert malformed.status_code == 422
        assert "source_ref" in malformed.json()["detail"]
        conversation = client.post(
            "/v1/conversations",
            headers=_headers(),
            json={"owner_id": "owner-a", "client_id": "test"},
        )
        retrieval = client.post(
            f"/v2/conversations/{conversation.json()['conversation_id']}/retrieve",
            headers=_headers("rid-malformed-retrieval"),
            json={
                "request_id": "rid-malformed-retrieval",
                "owner_id": "owner-a",
                "query": "malformed",
            },
        )
        assert retrieval.status_code == 200
        assert retrieval.json()["bundle"]["artifact_refs"] == []
        debug = retrieval.json()["bundle"]["retrieval_debug"]
        assert debug["malformed_artifact_provenance_count"] == 1
        assert "malformed_derivative_provenance" in debug["artifact_omission_reasons"]
