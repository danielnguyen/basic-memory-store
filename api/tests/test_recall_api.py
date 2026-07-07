import os
import types
import uuid

from fastapi.testclient import TestClient

os.environ.setdefault("MEMORY_API_KEY", "testkey")
os.environ.setdefault("PG_DSN", "postgresql://user:pass@localhost:5432/test")
os.environ.setdefault("QDRANT_URL", "http://localhost:6333")
os.environ.setdefault("LITELLM_BASE_URL", "http://localhost:4000")

import main as main_module
from services.recall import select_recall_decision


class FakePG:
    def __init__(self):
        self.rows = {}
        self.persist_calls = []

    async def open(self):
        return None

    async def close(self):
        return None

    async def persist_recall_decisions(self, **kwargs):
        self.persist_calls.append(kwargs)
        now = "2026-01-01T00:00:00+00:00"
        out = []
        for item in kwargs["decisions"]:
            key = (kwargs["request_id"], kwargs["owner_id"], item["candidate_type"], item["candidate_id"])
            existing = self.rows.get(key, {})
            row = {
                "id": existing.get("id") or str(uuid.uuid4()),
                "request_id": kwargs["request_id"],
                "owner_id": kwargs["owner_id"],
                "candidate_id": item["candidate_id"],
                "candidate_type": item["candidate_type"],
                "candidate_ref_json": item["candidate_ref_json"],
                "source_refs_json": item["source_refs_json"],
                "scene_id": item.get("scene_id"),
                "surface": item.get("surface"),
                "urgency": item.get("urgency"),
                "sensitivity": item.get("sensitivity"),
                "relevance_score": item["relevance_score"],
                "salience_score": item["salience_score"],
                "recency_score": item["recency_score"],
                "mentionability_score": item["mentionability_score"],
                "decision": item["decision"],
                "mention_strategy": item["mention_strategy"],
                "prompt_eligible": item["prompt_eligible"],
                "reason_json": item["reason_json"],
                "created_at": existing.get("created_at") or now,
            }
            self.rows[key] = row
            out.append(row)
        return out

    async def get_recall_debug(self, **kwargs):
        rows = [
            row
            for (request_id, owner_id, _candidate_type, _candidate_id), row in self.rows.items()
            if request_id == kwargs["request_id"] and owner_id == kwargs["owner_id"]
        ]
        return sorted(rows, key=lambda row: (row["created_at"], row["id"]))


class ExplodingQdrant:
    def ping(self):
        return True

    def __getattr__(self, name):
        raise AssertionError(f"recall selection must not call qdrant.{name}")


class ExplodingLiteLLM:
    def __getattr__(self, name):
        raise AssertionError(f"recall selection must not call litellm.{name}")


def _settings():
    return types.SimpleNamespace(
        memory_api_key="testkey",
        require_request_id=True,
        enforce_request_id_header_body_match=True,
    )


def test_recall_policy_is_deterministic_and_allows_explicit_callback():
    context = {"scene_id": "debugging", "surface": "vscode", "urgency": "medium", "sensitivity": "low"}
    candidate = {
        "candidate_id": "mem-1",
        "candidate_type": "memory_item",
        "summary": "Prior passthrough issue",
        "relevance_score": 0.86,
        "salience_score": 0.70,
        "recency_score": 0.40,
        "metadata": {"explicit_callback_allowed": True},
    }

    first = select_recall_decision(context=context, candidate=candidate)
    second = select_recall_decision(context=context, candidate=candidate)

    assert first == second
    assert first["mentionability_score"] == 0.751
    assert first["decision"] == "mention"
    assert first["mention_strategy"] == "light_callback"
    assert first["prompt_eligible"] is True
    assert first["reason_json"]["rule_id"] == "light_callback_allowed"


def test_recall_policy_suppresses_below_relevance_threshold():
    out = select_recall_decision(
        context={"surface": "vscode", "sensitivity": "low"},
        candidate={"candidate_id": "mem-1", "candidate_type": "memory_item", "relevance_score": 0.34},
    )

    assert out["decision"] == "suppress"
    assert out["mention_strategy"] == "none"
    assert out["prompt_eligible"] is False
    assert out["reason_json"]["rule_id"] == "below_relevance_threshold"


def test_recall_policy_high_sensitivity_suppresses_or_caps_to_implicit():
    suppressed = select_recall_decision(
        context={"surface": "vscode", "sensitivity": "high"},
        candidate={"candidate_id": "episode-1", "candidate_type": "episode", "relevance_score": 0.95},
    )
    capped = select_recall_decision(
        context={"surface": "vscode", "sensitivity": "high"},
        candidate={
            "candidate_id": "episode-1",
            "candidate_type": "episode",
            "relevance_score": 0.95,
            "metadata": {"allow_sensitive_mention": True, "explicit_callback_allowed": True},
        },
    )

    assert suppressed["decision"] == "suppress"
    assert suppressed["prompt_eligible"] is False
    assert suppressed["reason_json"]["rule_id"] == "high_sensitivity_suppression"
    assert capped["decision"] == "implicit_only"
    assert capped["mention_strategy"] == "implicit"
    assert capped["prompt_eligible"] is False
    assert capped["reason_json"]["rule_id"] == "high_sensitivity_implicit_cap"


def test_recall_policy_explicit_callback_requires_metadata_and_nonurgent_context():
    out = select_recall_decision(
        context={"surface": "vscode", "urgency": "low", "sensitivity": "low"},
        candidate={
            "candidate_id": "episode-1",
            "candidate_type": "episode",
            "relevance_score": 0.95,
            "salience_score": 0.9,
            "recency_score": 0.8,
            "metadata": {"explicit_callback_allowed": True},
        },
    )

    assert out["mentionability_score"] == 0.915
    assert out["decision"] == "mention"
    assert out["mention_strategy"] == "explicit_callback"
    assert out["prompt_eligible"] is True
    assert out["reason_json"]["rule_id"] == "explicit_callback_allowed"


def test_recall_select_persists_all_decisions_and_debug_returns_them(monkeypatch):
    pg = FakePG()
    monkeypatch.setattr(main_module, "settings", _settings(), raising=True)
    monkeypatch.setattr(main_module, "pg", pg, raising=True)
    monkeypatch.setattr(main_module, "qdrant", ExplodingQdrant(), raising=True)
    monkeypatch.setattr(main_module, "litellm", ExplodingLiteLLM(), raising=True)

    client = TestClient(main_module.app)
    try:
        body = {
            "request_id": "rid-recall-1",
            "owner_id": "owner",
            "context": {"scene_id": "debugging", "surface": "vscode", "urgency": "low", "sensitivity": "low"},
            "candidates": [
                {
                    "candidate_id": "mem-1",
                    "candidate_type": "memory_item",
                    "summary": "Prior driver passthrough issue",
                    "source_refs": [{"ref_type": "message", "ref_id": "m-2"}, {"ref_type": "message", "ref_id": "m-1"}],
                    "relevance_score": 0.95,
                    "salience_score": 0.9,
                    "recency_score": 0.8,
                    "metadata": {"explicit_callback_allowed": True},
                },
                {
                    "candidate_id": "episode-1",
                    "candidate_type": "episode",
                    "relevance_score": 0.2,
                    "salience_score": 1.0,
                    "recency_score": 1.0,
                },
            ],
        }
        response = client.post(
            "/v1/internal/recall/select",
            headers={"X-API-Key": "testkey", "X-Request-ID": "rid-recall-1"},
            json=body,
        )

        assert response.status_code == 200
        out = response.json()
        assert out["decision_count"] == 2
        assert out["decisions"][0]["mention_strategy"] == "explicit_callback"
        assert out["decisions"][0]["prompt_eligible"] is True
        assert [ref["ref_id"] for ref in pg.persist_calls[0]["decisions"][0]["source_refs_json"]] == ["m-1", "m-2"]
        assert out["decisions"][1]["decision"] == "suppress"
        assert out["decisions"][1]["prompt_eligible"] is False
        assert out["decisions"][1]["reason"]["rule_id"] == "below_relevance_threshold"

        second = client.post(
            "/v1/internal/recall/select",
            headers={"X-API-Key": "testkey", "X-Request-ID": "rid-recall-1"},
            json=body,
        )
        assert second.status_code == 200
        assert [d["id"] for d in second.json()["decisions"]] == [d["id"] for d in out["decisions"]]
        assert [d["decision"] for d in second.json()["decisions"]] == [d["decision"] for d in out["decisions"]]

        debug = client.get(
            "/v1/internal/recall/debug/rid-recall-1?owner_id=owner",
            headers={"X-API-Key": "testkey"},
        )
        assert debug.status_code == 200
        debug_body = debug.json()
        assert debug_body["request_id"] == "rid-recall-1"
        assert debug_body["context"] == {
            "scene_id": "debugging",
            "surface": "vscode",
            "urgency": "low",
            "sensitivity": "low",
        }
        assert debug_body["decision_count"] == 2
        assert {item["candidate_id"] for item in debug_body["decisions"]} == {"mem-1", "episode-1"}
    finally:
        client.close()


def test_recall_select_sensitive_context_never_prompt_eligible(monkeypatch):
    pg = FakePG()
    monkeypatch.setattr(main_module, "settings", _settings(), raising=True)
    monkeypatch.setattr(main_module, "pg", pg, raising=True)
    monkeypatch.setattr(main_module, "qdrant", ExplodingQdrant(), raising=True)

    client = TestClient(main_module.app)
    try:
        response = client.post(
            "/v1/internal/recall/select",
            headers={"X-API-Key": "testkey", "X-Request-ID": "rid-sensitive"},
            json={
                "request_id": "rid-sensitive",
                "owner_id": "owner",
                "context": {"surface": "vscode", "sensitivity": "high"},
                "candidates": [
                    {
                        "candidate_id": "mem-1",
                        "candidate_type": "memory_item",
                        "relevance_score": 0.99,
                        "metadata": {"allow_sensitive_mention": True, "explicit_callback_allowed": True},
                    }
                ],
            },
        )

        assert response.status_code == 200
        decision = response.json()["decisions"][0]
        assert decision["decision"] == "implicit_only"
        assert decision["mention_strategy"] == "implicit"
        assert decision["prompt_eligible"] is False
    finally:
        client.close()


def test_recall_debug_is_request_and_owner_scoped(monkeypatch):
    pg = FakePG()
    monkeypatch.setattr(main_module, "settings", _settings(), raising=True)
    monkeypatch.setattr(main_module, "pg", pg, raising=True)
    monkeypatch.setattr(main_module, "qdrant", ExplodingQdrant(), raising=True)

    client = TestClient(main_module.app)
    try:
        body = {
            "request_id": "rid-owner-scope",
            "owner_id": "owner-a",
            "context": {"surface": "chat", "urgency": "low", "sensitivity": "low"},
            "candidates": [
                {
                    "candidate_id": "mem-light",
                    "candidate_type": "memory_item",
                    "relevance_score": 0.76,
                    "salience_score": 0.62,
                    "source_refs": [{"ref_type": "message", "ref_id": "m-light"}],
                }
            ],
        }
        response = client.post(
            "/v1/internal/recall/select",
            headers={"X-API-Key": "testkey", "X-Request-ID": "rid-owner-scope"},
            json=body,
        )
        assert response.status_code == 200
        decision = response.json()["decisions"][0]
        assert decision["decision"] == "mention"
        assert decision["mention_strategy"] == "light_callback"
        assert decision["reason"]["rule_id"] == "light_callback_allowed"

        missing = client.get(
            "/v1/internal/recall/debug/rid-owner-scope?owner_id=owner-b",
            headers={"X-API-Key": "testkey"},
        )
        assert missing.status_code == 404

        present = client.get(
            "/v1/internal/recall/debug/rid-owner-scope?owner_id=owner-a",
            headers={"X-API-Key": "testkey"},
        )
        assert present.status_code == 200
        assert present.json()["decisions"][0]["source_refs"][0]["ref_id"] == "m-light"
    finally:
        client.close()
