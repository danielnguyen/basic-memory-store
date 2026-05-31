import types
import uuid

from test_proactive_api import FakePG, FakeQdrant, _client, _headers


def _git_event(fake_pg, *, owner_id="owner", event_log_id=None, title="auth flow refactor"):
    event_log_id = event_log_id or str(uuid.uuid4())
    fake_pg.event_logs[event_log_id] = {
        "event_log_id": event_log_id,
        "owner_id": owner_id,
        "source_type": "git",
        "source_event_id": f"git-{event_log_id}",
        "event_type": "push",
        "payload_json": {
            "summary": title,
            "title": title,
            "repo": "basic-memory-store",
            "branch": "main",
        },
        "conversation_id": str(uuid.uuid4()),
        "message_id": str(uuid.uuid4()),
    }
    return event_log_id


def _portfolio_event(fake_pg, *, owner_id="owner", event_log_id=None, drift=0.09):
    event_log_id = event_log_id or str(uuid.uuid4())
    fake_pg.event_logs[event_log_id] = {
        "event_log_id": event_log_id,
        "owner_id": owner_id,
        "source_type": "portfolio",
        "source_event_id": f"port-{event_log_id}",
        "event_type": "allocation_drift",
        "payload_json": {
            "account": "taxable account",
            "symbol": "NVDA",
            "allocation_drift_pct": drift,
            "summary": "NVDA overweight",
        },
        "conversation_id": str(uuid.uuid4()),
        "message_id": str(uuid.uuid4()),
    }
    return event_log_id


def _enable(client, *, rule_prefs=None, surfaces=None):
    return client.put(
        "/v1/proactive/preferences",
        headers={"X-API-Key": "testkey"},
        json={
            "owner_id": "owner",
            "enabled": True,
            "allowed_surfaces_json": surfaces if surfaces is not None else ["telegram"],
            "rule_prefs_json": rule_prefs or {},
        },
    )


def test_initiative_evaluate_records_created_decision_and_suggestion(monkeypatch):
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
        _enable(client, rule_prefs={"git": {"min_score": 0.3}})
        event_log_id = _git_event(fake_pg)
        r = client.post(
            "/v1/initiative/evaluate",
            headers=_headers("rid-initiative-created"),
            json={
                "request_id": "rid-initiative-created",
                "owner_id": "owner",
                "event_log_id": event_log_id,
                "surface": "telegram",
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["created_count"] == 1
        decision = body["decisions"][0]
        assert decision["decision_status"] == "created"
        assert decision["proactive_suggestion_id"] == body["suggestions"][0]["suggestion_id"]
        assert decision["cooldown_identity_key"].startswith("owner|git_risk_scan|telegram|git|")
        assert decision["reason_json"]["trigger_source"] == "git"
        assert decision["reason_json"]["scoring"]["threshold"] == 0.3
    finally:
        client.close()


def test_initiative_persists_no_allowed_surface_suppression(monkeypatch):
    client, fake_pg, _ = _client(monkeypatch)
    try:
        _enable(client, surfaces=[])
        event_log_id = _portfolio_event(fake_pg)
        r = client.post(
            "/v1/initiative/evaluate",
            headers=_headers("rid-no-surface"),
            json={"request_id": "rid-no-surface", "owner_id": "owner", "event_log_id": event_log_id},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["created_count"] == 0
        assert body["decisions"][0]["decision_status"] == "suppressed"
        assert body["decisions"][0]["suppression_reason"] == "no_allowed_surface"
        assert fake_pg.suggestions == {}
    finally:
        client.close()


def test_initiative_persists_no_matching_rule(monkeypatch):
    client, fake_pg, _ = _client(monkeypatch)
    try:
        _enable(client)
        event_log_id = str(uuid.uuid4())
        fake_pg.event_logs[event_log_id] = {
            "event_log_id": event_log_id,
            "owner_id": "owner",
            "source_type": "calendar",
            "source_event_id": "cal-1",
            "event_type": "meeting_created",
            "payload_json": {"summary": "planning"},
            "conversation_id": str(uuid.uuid4()),
            "message_id": str(uuid.uuid4()),
        }
        r = client.post(
            "/v1/initiative/evaluate",
            headers=_headers("rid-no-rule"),
            json={"request_id": "rid-no-rule", "owner_id": "owner", "event_log_id": event_log_id},
        )
        assert r.status_code == 200
        assert r.json()["decisions"][0]["decision_status"] == "no_op"
        assert r.json()["decisions"][0]["suppression_reason"] == "no_matching_rule"
    finally:
        client.close()


def test_initiative_cooldown_suppresses_same_subject_only(monkeypatch):
    client, fake_pg, _ = _client(monkeypatch)
    try:
        _enable(client, rule_prefs={"portfolio_drift_review": {"cooldown_hours": 24}})
        first_event = _portfolio_event(fake_pg, drift=0.09)
        first = client.post(
            "/v1/initiative/evaluate",
            headers=_headers("rid-cooldown-1"),
            json={"request_id": "rid-cooldown-1", "owner_id": "owner", "event_log_id": first_event},
        )
        assert first.status_code == 200
        assert first.json()["created_count"] == 1

        second_event = _portfolio_event(fake_pg, drift=0.1)
        second = client.post(
            "/v1/initiative/evaluate",
            headers=_headers("rid-cooldown-2"),
            json={"request_id": "rid-cooldown-2", "owner_id": "owner", "event_log_id": second_event},
        )
        assert second.status_code == 200
        decision = second.json()["decisions"][0]
        assert second.json()["created_count"] == 0
        assert decision["suppression_reason"] == "cooldown_active"
        assert decision["cooldown_identity_key"] == first.json()["decisions"][0]["cooldown_identity_key"]
    finally:
        client.close()


def test_initiative_evaluate_is_idempotent_for_same_request_id(monkeypatch):
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
        _enable(client, rule_prefs={"git": {"min_score": 0.3}})
        event_log_id = _git_event(fake_pg)
        first = client.post(
            "/v1/initiative/evaluate",
            headers=_headers("rid-idempotent"),
            json={
                "request_id": "rid-idempotent",
                "owner_id": "owner",
                "event_log_id": event_log_id,
                "surface": "telegram",
            },
        )
        second = client.post(
            "/v1/initiative/evaluate",
            headers=_headers("rid-idempotent"),
            json={
                "request_id": "rid-idempotent",
                "owner_id": "owner",
                "event_log_id": event_log_id,
                "surface": "telegram",
            },
        )
        assert first.status_code == 200
        assert second.status_code == 200
        first_body = first.json()
        second_body = second.json()
        assert first_body == second_body
        assert second_body["created_count"] == 1
        assert len(fake_pg.initiative_decisions) == 1
        assert len(fake_pg.suggestions) == 1
    finally:
        client.close()


def test_initiative_feedback_links_to_proactive_feedback_and_debug(monkeypatch):
    client, fake_pg, _ = _client(monkeypatch)
    try:
        _enable(client)
        event_log_id = _portfolio_event(fake_pg, drift=0.09)
        evaluated = client.post(
            "/v1/initiative/evaluate",
            headers=_headers("rid-feedback"),
            json={"request_id": "rid-feedback", "owner_id": "owner", "event_log_id": event_log_id},
        )
        assert evaluated.status_code == 200
        decision_id = evaluated.json()["decisions"][0]["decision_id"]
        feedback = client.post(
            "/v1/initiative/feedback",
            headers={"X-API-Key": "testkey"},
            json={
                "owner_id": "owner",
                "decision_id": decision_id,
                "feedback_type": "not_useful",
                "feedback_json": {"reason": "too soon"},
            },
        )
        assert feedback.status_code == 200
        assert feedback.json()["feedback_id"]
        assert feedback.json()["proactive_feedback_id"]
        assert fake_pg.feedback[0]["feedback_type"] == "not_useful"

        debug = client.get(
            "/v1/initiative/debug/rid-feedback",
            headers={"X-API-Key": "testkey"},
            params={"owner_id": "owner"},
        )
        assert debug.status_code == 200
        body = debug.json()
        assert body["initiative_event"]["request_id"] == "rid-feedback"
        assert body["decisions"][0]["decision_id"] == decision_id
        assert body["suggestions"][0]["suggestion_id"] == evaluated.json()["suggestions"][0]["suggestion_id"]
        assert body["feedback"][0]["feedback_type"] == "not_useful"
    finally:
        client.close()


def test_initiative_debug_is_owner_scoped(monkeypatch):
    client, fake_pg, _ = _client(monkeypatch)
    try:
        _enable(client)
        owner_event_id = _portfolio_event(fake_pg, owner_id="owner", drift=0.09)
        other_event_id = _portfolio_event(fake_pg, owner_id="other", drift=0.11)
        client.put(
            "/v1/proactive/preferences",
            headers={"X-API-Key": "testkey"},
            json={
                "owner_id": "other",
                "enabled": True,
                "allowed_surfaces_json": ["telegram"],
                "rule_prefs_json": {},
            },
        )
        owner_eval = client.post(
            "/v1/initiative/evaluate",
            headers=_headers("rid-shared"),
            json={"request_id": "rid-shared", "owner_id": "owner", "event_log_id": owner_event_id},
        )
        other_eval = client.post(
            "/v1/initiative/evaluate",
            headers={"X-API-Key": "testkey", "X-Request-ID": "rid-shared"},
            json={"request_id": "rid-shared", "owner_id": "other", "event_log_id": other_event_id},
        )
        assert owner_eval.status_code == 200
        assert other_eval.status_code == 200

        owner_debug = client.get(
            "/v1/initiative/debug/rid-shared",
            headers={"X-API-Key": "testkey"},
            params={"owner_id": "owner"},
        )
        other_debug = client.get(
            "/v1/initiative/debug/rid-shared",
            headers={"X-API-Key": "testkey"},
            params={"owner_id": "other"},
        )
        assert owner_debug.status_code == 200
        assert other_debug.status_code == 200
        assert owner_debug.json()["initiative_event"]["owner_id"] == "owner"
        assert other_debug.json()["initiative_event"]["owner_id"] == "other"
        assert owner_debug.json()["initiative_event"]["initiative_event_id"] != other_debug.json()["initiative_event"]["initiative_event_id"]
    finally:
        client.close()
