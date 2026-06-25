from __future__ import annotations

import types
from uuid import uuid4

from fastapi.testclient import TestClient
import psycopg

import main as main_module
from services.memory_lifecycle import DURABLE_MEMORY_STATUSES
from storage.postgres import PostgresStore


class FakeQdrant:
    def ping(self):
        return True


def _settings():
    return types.SimpleNamespace(
        memory_api_key="testkey",
        require_request_id=True,
        enforce_request_id_header_body_match=True,
    )


def _promote(client: TestClient, *, request_id: str, owner_id: str, ref_id: str) -> dict:
    response = client.post(
        "/v1/internal/memory/promote",
        headers={"X-API-Key": "testkey", "X-Request-ID": request_id},
        json={
            "request_id": request_id,
            "owner_id": owner_id,
            "memory_type": "preference",
            "summary": f"private summary for {ref_id}",
            "source_refs": [{"ref_type": "message", "ref_id": ref_id}],
            "explanation": {"rationale": "explicit source"},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["memory"]


def _transition(
    client: TestClient,
    *,
    memory_id: str,
    request_id: str,
    owner_id: str,
    status: str,
    related_memory_id: str | None = None,
):
    payload = {
        "request_id": request_id,
        "owner_id": owner_id,
        "status": status,
        "reason": {
            "code": "lifecycle_review",
            "metadata": {
                "source": "operator",
                "nested_private_content": {"text": "must not be persisted"},
            },
        },
    }
    if related_memory_id is not None:
        payload["related_memory_id"] = related_memory_id
    return client.post(
        f"/v1/internal/memory/{memory_id}/transition",
        headers={"X-API-Key": "testkey", "X-Request-ID": request_id},
        json=payload,
    )


def test_every_durable_status_round_trips_on_clean_postgresql_16(postgres_database):
    with psycopg.connect(postgres_database) as conn:
        for index, status in enumerate(DURABLE_MEMORY_STATUSES):
            memory_id = uuid4()
            conn.execute(
                """
                INSERT INTO memory_items (
                    id, owner_id, memory_type, summary, source_ref_hash,
                    promotion_state, status
                ) VALUES (%s, 'owner-status', 'fixture', 'bounded fixture', %s, 'promoted', %s)
                """,
                (memory_id, f"hash-{index}", status),
            )
        rows = conn.execute(
            """
            SELECT status
            FROM memory_items
            WHERE owner_id = 'owner-status'
            ORDER BY source_ref_hash
            """
        ).fetchall()

    assert {row[0] for row in rows} == set(DURABLE_MEMORY_STATUSES)


def test_transition_audit_relationships_owner_scope_idempotency_and_reopen(
    monkeypatch,
    postgres_database,
):
    monkeypatch.setattr(main_module, "settings", _settings(), raising=True)
    monkeypatch.setattr(main_module, "qdrant", FakeQdrant(), raising=True)
    first_store = PostgresStore(postgres_database)
    monkeypatch.setattr(main_module, "pg", first_store, raising=True)

    with TestClient(main_module.app) as client:
        original = _promote(client, request_id="rid-original", owner_id="owner-a", ref_id="message-original")
        replacement = _promote(client, request_id="rid-replacement", owner_id="owner-a", ref_id="message-replacement")
        other = _promote(client, request_id="rid-other", owner_id="owner-a", ref_id="message-other")
        foreign = _promote(client, request_id="rid-foreign", owner_id="owner-b", ref_id="message-foreign")

        stale = _transition(
            client,
            memory_id=original["memory_id"],
            request_id="rid-stale",
            owner_id="owner-a",
            status="stale",
        )
        assert stale.status_code == 200, stale.text
        assert stale.json()["memory"]["status"] == "stale"
        assert stale.json()["memory"]["freshness_state"] == "stale"

        repeated = _transition(
            client,
            memory_id=original["memory_id"],
            request_id="rid-stale-repeat",
            owner_id="owner-a",
            status="stale",
        )
        assert repeated.status_code == 200
        assert repeated.json()["changed"] is False
        assert repeated.json()["events_appended"] == []

        cross_owner = _transition(
            client,
            memory_id=original["memory_id"],
            request_id="rid-cross-owner",
            owner_id="owner-b",
            status="retracted",
        )
        assert cross_owner.status_code == 404

        missing_related = _transition(
            client,
            memory_id=replacement["memory_id"],
            request_id="rid-missing-related",
            owner_id="owner-a",
            status="corrected",
            related_memory_id=str(uuid4()),
        )
        assert missing_related.status_code == 404

        cross_owner_related = _transition(
            client,
            memory_id=replacement["memory_id"],
            request_id="rid-cross-owner-related",
            owner_id="owner-a",
            status="corrected",
            related_memory_id=foreign["memory_id"],
        )
        assert cross_owner_related.status_code == 404

        corrected = _transition(
            client,
            memory_id=replacement["memory_id"],
            request_id="rid-corrected",
            owner_id="owner-a",
            status="corrected",
            related_memory_id=original["memory_id"],
        )
        assert corrected.status_code == 200, corrected.text
        corrected_memory = corrected.json()["memory"]
        assert corrected_memory["status"] == "corrected"
        assert corrected_memory["freshness_state"] == "corrected"
        assert corrected_memory["supersedes_memory_id"] == original["memory_id"]

        conflicting = _transition(
            client,
            memory_id=other["memory_id"],
            request_id="rid-conflict",
            owner_id="owner-a",
            status="corrected",
            related_memory_id=original["memory_id"],
        )
        assert conflicting.status_code == 409

        superseded_item = _promote(
            client,
            request_id="rid-explicit-superseded",
            owner_id="owner-a",
            ref_id="message-explicit-superseded",
        )
        superseding_item = _promote(
            client,
            request_id="rid-explicit-superseding",
            owner_id="owner-a",
            ref_id="message-explicit-superseding",
        )
        superseded = _transition(
            client,
            memory_id=superseded_item["memory_id"],
            request_id="rid-explicit-supersession",
            owner_id="owner-a",
            status="superseded",
            related_memory_id=superseding_item["memory_id"],
        )
        assert superseded.status_code == 200
        assert superseded.json()["memory"]["superseded_by_memory_id"] == superseding_item["memory_id"]
        superseding_debug = client.get(
            f"/v1/internal/memory/{superseding_item['memory_id']}/debug?owner_id=owner-a",
            headers={"X-API-Key": "testkey"},
        ).json()
        assert superseding_debug["memory"]["supersedes_memory_id"] == superseded_item["memory_id"]

        debug_original = client.get(
            f"/v1/internal/memory/{original['memory_id']}/debug?owner_id=owner-a",
            headers={"X-API-Key": "testkey"},
        )
        assert debug_original.status_code == 200
        original_body = debug_original.json()
        assert original_body["memory"]["status"] == "superseded"
        assert original_body["memory"]["superseded_by_memory_id"] == replacement["memory_id"]
        lifecycle_events = [
            event for event in original_body["events"] if event["event_type"] == "state_changed"
        ]
        assert [(event["reason"]["previous_status"], event["reason"]["new_status"]) for event in lifecycle_events] == [
            ("active", "stale"),
            ("stale", "superseded"),
        ]
        assert all("private summary" not in str(event["reason"]) for event in lifecycle_events)
        assert all("nested_private_content" not in event["reason"].get("reason_metadata", {}) for event in lifecycle_events)

        hidden = client.get(
            f"/v1/internal/memory/{original['memory_id']}/debug?owner_id=owner-b",
            headers={"X-API-Key": "testkey"},
        )
        assert hidden.status_code == 404

    second_store = PostgresStore(postgres_database)
    monkeypatch.setattr(main_module, "pg", second_store, raising=True)
    with TestClient(main_module.app) as reopened_client:
        persisted = reopened_client.get(
            f"/v1/internal/memory/{replacement['memory_id']}/debug?owner_id=owner-a",
            headers={"X-API-Key": "testkey"},
        )
        assert persisted.status_code == 200
        body = persisted.json()
        assert body["memory"]["status"] == "corrected"
        assert body["memory"]["supersedes_memory_id"] == original["memory_id"]
        assert any(event["reason"].get("request_id") == "rid-corrected" for event in body["events"])


def test_rejected_transition_is_atomic_and_appends_no_success_event(monkeypatch, postgres_database):
    monkeypatch.setattr(main_module, "settings", _settings(), raising=True)
    monkeypatch.setattr(main_module, "qdrant", FakeQdrant(), raising=True)
    store = PostgresStore(postgres_database)
    monkeypatch.setattr(main_module, "pg", store, raising=True)

    with TestClient(main_module.app) as client:
        memory = _promote(client, request_id="rid-atomic-create", owner_id="owner-a", ref_id="message-atomic")
        before = client.get(
            f"/v1/internal/memory/{memory['memory_id']}/debug?owner_id=owner-a",
            headers={"X-API-Key": "testkey"},
        ).json()
        rejected = _transition(
            client,
            memory_id=memory["memory_id"],
            request_id="rid-atomic-reject",
            owner_id="owner-a",
            status="corrected",
        )
        assert rejected.status_code == 409
        after = client.get(
            f"/v1/internal/memory/{memory['memory_id']}/debug?owner_id=owner-a",
            headers={"X-API-Key": "testkey"},
        ).json()

    assert after["memory"]["status"] == before["memory"]["status"] == "active"
    assert len(after["events"]) == len(before["events"])


def test_existing_promote_supersession_remains_atomic_and_auditable(monkeypatch, postgres_database):
    monkeypatch.setattr(main_module, "settings", _settings(), raising=True)
    monkeypatch.setattr(main_module, "qdrant", FakeQdrant(), raising=True)
    store = PostgresStore(postgres_database)
    monkeypatch.setattr(main_module, "pg", store, raising=True)

    with TestClient(main_module.app) as client:
        original = _promote(
            client,
            request_id="rid-promote-original",
            owner_id="owner-a",
            ref_id="message-promote-original",
        )
        response = client.post(
            "/v1/internal/memory/promote",
            headers={"X-API-Key": "testkey", "X-Request-ID": "rid-promote-replacement"},
            json={
                "request_id": "rid-promote-replacement",
                "owner_id": "owner-a",
                "memory_type": "preference",
                "summary": "replacement memory",
                "source_refs": [{"ref_type": "message", "ref_id": "message-promote-replacement"}],
                "supersedes_memory_id": original["memory_id"],
            },
        )
        assert response.status_code == 200, response.text
        replacement = response.json()["memory"]
        assert replacement["supersedes_memory_id"] == original["memory_id"]

        original_debug = client.get(
            f"/v1/internal/memory/{original['memory_id']}/debug?owner_id=owner-a",
            headers={"X-API-Key": "testkey"},
        ).json()
        assert original_debug["memory"]["superseded_by_memory_id"] == replacement["memory_id"]
        superseded_event = next(
            event for event in original_debug["events"] if event["event_type"] == "superseded"
        )
        assert superseded_event["reason"]["previous_status"] == "active"
        assert superseded_event["reason"]["new_status"] == "superseded"

        conflict = client.post(
            "/v1/internal/memory/promote",
            headers={"X-API-Key": "testkey", "X-Request-ID": "rid-promote-conflict"},
            json={
                "request_id": "rid-promote-conflict",
                "owner_id": "owner-a",
                "memory_type": "preference",
                "summary": "conflicting replacement",
                "source_refs": [{"ref_type": "message", "ref_id": "message-promote-conflict"}],
                "supersedes_memory_id": original["memory_id"],
            },
        )
        assert conflict.status_code == 409


def test_event_storage_failure_rolls_back_status_change(monkeypatch, postgres_database):
    monkeypatch.setattr(main_module, "settings", _settings(), raising=True)
    monkeypatch.setattr(main_module, "qdrant", FakeQdrant(), raising=True)
    store = PostgresStore(postgres_database)
    monkeypatch.setattr(main_module, "pg", store, raising=True)

    with TestClient(main_module.app) as setup_client:
        memory = _promote(
            setup_client,
            request_id="rid-storage-failure-create",
            owner_id="owner-a",
            ref_id="message-storage-failure",
        )

    with psycopg.connect(postgres_database) as conn:
        conn.execute(
            """
            CREATE FUNCTION reject_lifecycle_event() RETURNS trigger AS $$
            BEGIN
              IF NEW.event_type = 'state_changed' THEN
                RAISE EXCEPTION 'injected lifecycle event failure';
              END IF;
              RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            CREATE TRIGGER reject_lifecycle_event_trigger
              BEFORE INSERT ON memory_events
              FOR EACH ROW EXECUTE FUNCTION reject_lifecycle_event();
            """
        )
        conn.commit()

    failure_store = PostgresStore(postgres_database)
    monkeypatch.setattr(main_module, "pg", failure_store, raising=True)
    with TestClient(main_module.app, raise_server_exceptions=False) as client:
        failed = _transition(
            client,
            memory_id=memory["memory_id"],
            request_id="rid-storage-failure",
            owner_id="owner-a",
            status="stale",
        )
        assert failed.status_code == 500

    with psycopg.connect(postgres_database) as conn:
        row = conn.execute(
            "SELECT status FROM memory_items WHERE id = %s",
            (memory["memory_id"],),
        ).fetchone()
        event_count = conn.execute(
            """
            SELECT count(*) FROM memory_events
            WHERE memory_id = %s AND event_type = 'state_changed'
            """,
            (memory["memory_id"],),
        ).fetchone()[0]

    assert row[0] == "active"
    assert event_count == 0
