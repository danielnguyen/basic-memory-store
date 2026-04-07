import types
import uuid

from fastapi.testclient import TestClient

import main as main_module


class FakePG:
    def __init__(self):
        self.prefs = {}
        self.event_logs = {}
        self.suggestions = {}
        self.feedback = []
        self.message_snippets = {}

    async def open(self):
        return None

    async def close(self):
        return None

    async def ping(self):
        return True

    async def get_proactive_prefs(self, owner_id):
        return self.prefs.get(owner_id)

    async def upsert_proactive_prefs(self, *, owner_id, enabled, allowed_surfaces_json, rule_prefs_json):
        row = {
            "owner_id": owner_id,
            "enabled": enabled,
            "allowed_surfaces_json": list(allowed_surfaces_json),
            "rule_prefs_json": dict(rule_prefs_json),
            "created_at": self.prefs.get(owner_id, {}).get("created_at", "2026-04-06T00:00:00+00:00"),
            "updated_at": "2026-04-06T00:00:00+00:00",
        }
        self.prefs[owner_id] = row
        return row.copy()

    async def get_event_ingest_log(self, event_log_id):
        return self.event_logs.get(str(event_log_id))

    async def resolve_profile(self, **kwargs):
        return {
            "profile_name": "dev",
            "source": "global_default",
            "profile_version": 1,
            "effective_profile_ref": f"{kwargs['owner_id']}:dev:1",
            "prompt_overlay": "",
            "retrieval_policy": {},
            "routing_policy": {},
            "response_style": {},
            "safety_policy": {},
            "tool_policy": {},
        }

    async def get_message_snippets_by_ids(self, ids):
        return [self.message_snippets[str(item)] for item in ids if str(item) in self.message_snippets]

    async def create_proactive_suggestion(self, **kwargs):
        key = (kwargs["owner_id"], str(kwargs["source_event_log_id"]), kwargs["kind"])
        existing = None
        for row in self.suggestions.values():
            if (row["owner_id"], row["source_event_log_id"], row["kind"]) == key:
                existing = row
                break
        if existing is not None:
            existing["explanation_json"] = kwargs["explanation_json"]
            existing["evidence_json"] = kwargs["evidence_json"]
            existing["target_surface"] = kwargs["target_surface"]
            existing["updated_at"] = "2026-04-06T00:00:00+00:00"
            return existing.copy(), False
        suggestion_id = str(uuid.uuid4())
        row = {
            "suggestion_id": suggestion_id,
            "owner_id": kwargs["owner_id"],
            "source_event_log_id": str(kwargs["source_event_log_id"]) if kwargs["source_event_log_id"] else None,
            "source_type": kwargs["source_type"],
            "kind": kwargs["kind"],
            "status": "pending",
            "title": kwargs["title"],
            "body": kwargs["body"],
            "explanation_json": kwargs["explanation_json"],
            "evidence_json": kwargs["evidence_json"],
            "target_surface": kwargs["target_surface"],
            "delivery_surface": None,
            "delivery_status": "not_attempted",
            "delivery_external_id": None,
            "delivery_error": None,
            "delivered_at": None,
            "created_at": "2026-04-06T00:00:00+00:00",
            "updated_at": "2026-04-06T00:00:00+00:00",
        }
        self.suggestions[suggestion_id] = row
        return row.copy(), True

    async def list_proactive_suggestions(self, *, owner_id, status=None, surface=None, delivery_status=None):
        rows = [row.copy() for row in self.suggestions.values() if row["owner_id"] == owner_id]
        if status is not None:
            rows = [row for row in rows if row["status"] == status]
        if surface is not None:
            rows = [row for row in rows if row["target_surface"] == surface]
        if delivery_status is not None:
            rows = [row for row in rows if row["delivery_status"] == delivery_status]
        rows.sort(key=lambda row: row["created_at"], reverse=True)
        return rows

    async def get_proactive_suggestion(self, suggestion_id):
        return self.suggestions.get(str(suggestion_id), None)

    async def record_proactive_feedback(self, *, suggestion_id, owner_id, feedback_type, reason):
        row = self.suggestions[str(suggestion_id)]
        if feedback_type == "dismissed":
            row["status"] = "dismissed"
        elif feedback_type == "accepted":
            row["status"] = "accepted"
        row["updated_at"] = "2026-04-06T00:00:00+00:00"
        feedback_id = str(uuid.uuid4())
        feedback = {
            "feedback_id": feedback_id,
            "suggestion_id": str(suggestion_id),
            "owner_id": owner_id,
            "feedback_type": feedback_type,
            "reason": reason,
            "status": row["status"],
            "created_at": "2026-04-06T00:00:00+00:00",
        }
        self.feedback.append(feedback)
        return feedback

    async def record_proactive_delivery_attempt(self, *, suggestion_id, owner_id, surface, delivery_status, external_id, error):
        row = self.suggestions.get(str(suggestion_id))
        if row is None or row["owner_id"] != owner_id:
            return None
        row["delivery_surface"] = surface
        row["delivery_status"] = delivery_status
        row["delivery_external_id"] = external_id
        row["delivery_error"] = error
        row["delivered_at"] = "2026-04-06T00:00:00+00:00" if delivery_status == "delivered" else None
        row["updated_at"] = "2026-04-06T00:00:00+00:00"
        return {
            "suggestion_id": row["suggestion_id"],
            "owner_id": row["owner_id"],
            "status": row["status"],
            "delivery_status": row["delivery_status"],
            "delivery_surface": row["delivery_surface"],
            "delivery_external_id": row["delivery_external_id"],
            "delivery_error": row["delivery_error"],
            "delivered_at": row["delivered_at"],
            "updated_at": row["updated_at"],
        }


class FakeQdrant:
    def __init__(self, *, search_hits=None):
        self.search_hits = search_hits or []

    def ping(self):
        return True

    async def search(self, **kwargs):
        return list(self.search_hits)

    async def search_artifact_chunks(self, **kwargs):
        return []


def _settings():
    return types.SimpleNamespace(
        memory_api_key="testkey",
        require_request_id=True,
        enforce_request_id_header_body_match=True,
        default_profile_name="dev",
        retrieval_recent_half_life_days=14,
        retrieval_balanced_half_life_days=45,
        retrieval_historical_half_life_days=365,
        retrieval_conversation_boost=0.08,
        retrieval_pinned_bias=0.12,
        retrieval_missing_penalty_cap=0.15,
        retrieval_artifact_k=3,
        retrieval_artifact_max_snippet_chars=500,
        recent_turns=10,
        enable_profile_resolve=True,
    )


def _client(monkeypatch, *, fake_pg=None, fake_qdrant=None):
    fake_pg = fake_pg or FakePG()
    fake_qdrant = fake_qdrant or FakeQdrant()
    monkeypatch.setattr(main_module, "settings", _settings(), raising=True)
    monkeypatch.setattr(main_module, "pg", fake_pg, raising=True)
    monkeypatch.setattr(main_module, "qdrant", fake_qdrant, raising=True)
    client = TestClient(main_module.app)
    return client, fake_pg, fake_qdrant


def _headers(request_id: str):
    return {"X-API-Key": "testkey", "X-Request-ID": request_id}


def test_proactive_disabled_skips_suggestion_creation(monkeypatch):
    client, fake_pg, _ = _client(monkeypatch)
    try:
        event_log_id = str(uuid.uuid4())
        fake_pg.event_logs[event_log_id] = {
            "event_log_id": event_log_id,
            "owner_id": "owner",
            "source_type": "git",
            "source_event_id": "git-1",
            "event_type": "push",
            "payload_json": {"summary": "touch auth flow", "repo": "basic-memory-store"},
            "conversation_id": str(uuid.uuid4()),
            "message_id": str(uuid.uuid4()),
        }
        r = client.post(
            "/v1/internal/proactive/evaluate",
            headers=_headers("rid-disabled"),
            json={"request_id": "rid-disabled", "owner_id": "owner", "event_log_id": event_log_id},
        )
        assert r.status_code == 200
        assert r.json()["created_count"] == 0
        assert fake_pg.suggestions == {}
    finally:
        client.close()


def test_list_suggestions_can_filter_by_delivery_status(monkeypatch):
    client, fake_pg, _ = _client(monkeypatch)
    try:
        not_attempted, _ = awaitable(fake_pg.create_proactive_suggestion(
            owner_id="owner",
            source_event_log_id=uuid.uuid4(),
            source_type="git",
            kind="git_risk_scan",
            title="pending unsent",
            body="body",
            explanation_json={},
            evidence_json={},
            target_surface="telegram",
        ))
        delivered, _ = awaitable(fake_pg.create_proactive_suggestion(
            owner_id="owner",
            source_event_log_id=uuid.uuid4(),
            source_type="git",
            kind="git_risk_scan",
            title="pending delivered",
            body="body",
            explanation_json={},
            evidence_json={},
            target_surface="telegram",
        ))
        awaitable(fake_pg.record_proactive_delivery_attempt(
            suggestion_id=uuid.UUID(delivered["suggestion_id"]),
            owner_id="owner",
            surface="telegram",
            delivery_status="delivered",
            external_id="nr-delivered",
            error=None,
        ))

        unfiltered = client.get(
            "/v1/proactive/suggestions",
            headers={"X-API-Key": "testkey"},
            params={"owner_id": "owner", "status": "pending", "surface": "telegram"},
        )
        assert unfiltered.status_code == 200
        assert {item["suggestion_id"] for item in unfiltered.json()["suggestions"]} == {
            not_attempted["suggestion_id"],
            delivered["suggestion_id"],
        }

        filtered = client.get(
            "/v1/proactive/suggestions",
            headers={"X-API-Key": "testkey"},
            params={
                "owner_id": "owner",
                "status": "pending",
                "surface": "telegram",
                "delivery_status": "not_attempted",
            },
        )
        assert filtered.status_code == 200
        suggestions = filtered.json()["suggestions"]
        assert [item["suggestion_id"] for item in suggestions] == [not_attempted["suggestion_id"]]
        assert suggestions[0]["delivery_status"] == "not_attempted"
    finally:
        client.close()


def test_git_event_creates_surface_aware_pending_suggestion(monkeypatch):
    fake_pg = FakePG()
    match_id = uuid.uuid4()
    fake_pg.message_snippets[str(match_id)] = {
        "message_id": str(match_id),
        "conversation_id": str(uuid.uuid4()),
        "role": "assistant",
        "content": "We discussed auth regressions in this repo last month.",
        "metadata": {},
        "created_at": "2026-03-20T00:00:00+00:00",
    }
    fake_qdrant = FakeQdrant(search_hits=[types.SimpleNamespace(message_id=str(match_id), score=0.82)])
    client, fake_pg, _ = _client(monkeypatch, fake_pg=fake_pg, fake_qdrant=fake_qdrant)
    try:
        client.put(
            "/v1/proactive/preferences",
            headers={"X-API-Key": "testkey"},
            json={
                "owner_id": "owner",
                "enabled": True,
                "allowed_surfaces_json": ["telegram"],
                "rule_prefs_json": {"git": {"min_score": 0.3}},
            },
        )
        event_log_id = str(uuid.uuid4())
        fake_pg.event_logs[event_log_id] = {
            "event_log_id": event_log_id,
            "owner_id": "owner",
            "source_type": "git",
            "source_event_id": "git-2",
            "event_type": "push",
            "payload_json": {"summary": "auth flow refactor", "repo": "basic-memory-store", "branch": "main"},
            "conversation_id": str(uuid.uuid4()),
            "message_id": str(uuid.uuid4()),
        }
        r = client.post(
            "/v1/internal/proactive/evaluate",
            headers=_headers("rid-git"),
            json={"request_id": "rid-git", "owner_id": "owner", "event_log_id": event_log_id},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["created_count"] == 1
        suggestion = body["suggestions"][0]
        assert suggestion["status"] == "pending"
        assert suggestion["delivery_status"] == "not_attempted"
        assert suggestion["target_surface"] == "telegram"
        assert suggestion["explanation_json"]["rule"] == "git_risk_scan"
        assert suggestion["evidence_json"]["matched_message"]["message_id"] == str(match_id)

        listed = client.get(
            "/v1/proactive/suggestions",
            headers={"X-API-Key": "testkey"},
            params={"owner_id": "owner", "status": "pending", "surface": "telegram"},
        )
        assert listed.status_code == 200
        assert len(listed.json()["suggestions"]) == 1
    finally:
        client.close()


def test_portfolio_drift_creates_explainable_suggestion(monkeypatch):
    client, fake_pg, _ = _client(monkeypatch)
    try:
        client.put(
            "/v1/proactive/preferences",
            headers={"X-API-Key": "testkey"},
            json={
                "owner_id": "owner",
                "enabled": True,
                "allowed_surfaces_json": ["telegram"],
                "rule_prefs_json": {"portfolio": {"drift_threshold": 0.05}},
            },
        )
        event_log_id = str(uuid.uuid4())
        fake_pg.event_logs[event_log_id] = {
            "event_log_id": event_log_id,
            "owner_id": "owner",
            "source_type": "portfolio",
            "source_event_id": "port-1",
            "event_type": "allocation_drift",
            "payload_json": {"account": "taxable account", "allocation_drift_pct": 0.09, "summary": "NVDA overweight"},
            "conversation_id": str(uuid.uuid4()),
            "message_id": str(uuid.uuid4()),
        }
        r = client.post(
            "/v1/internal/proactive/evaluate",
            headers=_headers("rid-port"),
            json={"request_id": "rid-port", "owner_id": "owner", "event_log_id": event_log_id},
        )
        assert r.status_code == 200
        suggestion = r.json()["suggestions"][0]
        assert suggestion["kind"] == "portfolio_drift_review"
        assert suggestion["explanation_json"]["observed_drift"] == 0.09
        assert suggestion["explanation_json"]["threshold"] == 0.05
    finally:
        client.close()


def test_delivery_attempt_updates_transport_state_only(monkeypatch):
    client, fake_pg, _ = _client(monkeypatch)
    try:
        row, _ = awaitable(fake_pg.create_proactive_suggestion(
            owner_id="owner",
            source_event_log_id=uuid.uuid4(),
            source_type="git",
            kind="git_risk_scan",
            title="title",
            body="body",
            explanation_json={},
            evidence_json={},
            target_surface="telegram",
        ))
        delivered = client.post(
            f"/v1/proactive/suggestions/{row['suggestion_id']}/delivery-attempt",
            headers={"X-API-Key": "testkey"},
            json={"owner_id": "owner", "surface": "telegram", "status": "delivered", "external_id": "nr-1"},
        )
        assert delivered.status_code == 200
        body = delivered.json()
        assert body["status"] == "pending"
        assert body["delivery_status"] == "delivered"
        assert body["delivery_external_id"] == "nr-1"

        failed = client.post(
            f"/v1/proactive/suggestions/{row['suggestion_id']}/delivery-attempt",
            headers={"X-API-Key": "testkey"},
            json={"owner_id": "owner", "surface": "telegram", "status": "failed", "error": "node-red timeout"},
        )
        assert failed.status_code == 200
        failed_body = failed.json()
        assert failed_body["status"] == "pending"
        assert failed_body["delivery_status"] == "failed"
        assert failed_body["delivery_error"] == "node-red timeout"
    finally:
        client.close()


def test_feedback_remains_user_feedback_only(monkeypatch):
    client, fake_pg, _ = _client(monkeypatch)
    try:
        row, _ = awaitable(fake_pg.create_proactive_suggestion(
            owner_id="owner",
            source_event_log_id=uuid.uuid4(),
            source_type="portfolio",
            kind="portfolio_drift_review",
            title="title",
            body="body",
            explanation_json={},
            evidence_json={},
            target_surface="telegram",
        ))
        useful = client.post(
            f"/v1/proactive/suggestions/{row['suggestion_id']}/feedback",
            headers={"X-API-Key": "testkey"},
            json={"owner_id": "owner", "feedback_type": "useful"},
        )
        assert useful.status_code == 200
        assert useful.json()["status"] == "pending"

        not_useful = client.post(
            f"/v1/proactive/suggestions/{row['suggestion_id']}/feedback",
            headers={"X-API-Key": "testkey"},
            json={"owner_id": "owner", "feedback_type": "not_useful"},
        )
        assert not_useful.status_code == 200
        assert not_useful.json()["status"] == "pending"

        dismissed = client.post(
            f"/v1/proactive/suggestions/{row['suggestion_id']}/feedback",
            headers={"X-API-Key": "testkey"},
            json={"owner_id": "owner", "feedback_type": "dismissed", "reason": "not now"},
        )
        assert dismissed.status_code == 200
        assert dismissed.json()["status"] == "dismissed"
        assert fake_pg.suggestions[row["suggestion_id"]]["delivery_status"] == "not_attempted"
    finally:
        client.close()


def awaitable(coro):
    import asyncio
    return asyncio.run(coro)
