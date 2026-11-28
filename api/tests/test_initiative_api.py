import asyncio
import json
import types
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest

import main as main_module
from services import proactive
from test_proactive_api import FakePG, FakeQdrant, _client, _headers, _settings

RECENT_MESSAGE_TS = (datetime.now(UTC) - timedelta(days=7)).isoformat()


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
        "created_at": RECENT_MESSAGE_TS,
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
        "created_at": RECENT_MESSAGE_TS,
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


# Paging uses synthetic data and a mock HTTP transport; it never contacts ntfy.
def _paging_settings(**overrides):
    settings = _settings()
    settings.proactive_ntfy_enabled = True
    settings.proactive_ntfy_base_url = "https://ntfy.example"
    settings.proactive_ntfy_topic = "ccp-pages"
    settings.proactive_ntfy_owner_id = "owner"
    settings.proactive_ntfy_timeout_s = 0.1
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


def _mock_paging(monkeypatch, pg, outcome="accepted"):
    requests = []
    original = httpx.AsyncClient

    async def handle(request):
        payload = json.loads(request.content)
        assert payload == {
            "version": 1, "suggestion_id": payload["suggestion_id"], "owner_id": "owner",
            "text": "CCP has a new suggestion for your attention. Open Vega to discuss it.",
        }
        assert str(request.url) == "https://ntfy.example/ccp-pages"
        assert request.method == "POST"
        assert payload["suggestion_id"] in pg.suggestions
        assert any(d["proactive_suggestion_id"] == payload["suggestion_id"] for d in pg.initiative_decisions.values())
        requests.append(request)
        if outcome == "timeout":
            raise httpx.ReadTimeout("synthetic timeout")
        if outcome == "slow":
            await asyncio.sleep(1)
        if outcome == "invalid":
            return httpx.Response(200, json={"event": "message", "topic": "wrong"})
        if outcome == "oversized":
            return httpx.Response(200, content=b"x" * 4097)
        if outcome == "error":
            return httpx.Response(503)
        if outcome == "redirect":
            return httpx.Response(307, headers={"location": "https://other.example"})
        return httpx.Response(200, json={"id": "synthetic", "topic": "ccp-pages", "event": "message"})

    monkeypatch.setattr(proactive.httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(handle), **kwargs,
    ))
    return requests


@pytest.mark.parametrize("source", ["git", "portfolio"])
@pytest.mark.parametrize("outcome", ["accepted", "error", "timeout", "slow", "invalid", "oversized", "redirect"])
def test_paging_persists_before_publish_and_never_claims_delivery(monkeypatch, source, outcome):
    pg = FakePG()
    match_id = str(uuid.uuid4())
    pg.message_snippets[match_id] = {
        "message_id": match_id, "conversation_id": str(uuid.uuid4()), "role": "assistant",
        "content": "Private synthetic memory must not appear in a page.",
        "metadata": {}, "created_at": RECENT_MESSAGE_TS,
    }
    qdrant = FakeQdrant(search_hits=[types.SimpleNamespace(message_id=match_id, score=0.82)])
    client, _, _ = _client(monkeypatch, fake_pg=pg, fake_qdrant=qdrant)
    monkeypatch.setattr(main_module, "settings", _paging_settings())
    requests = _mock_paging(monkeypatch, pg, outcome)
    try:
        _enable(client)
        event = _git_event(pg) if source == "git" else _portfolio_event(pg)
        data = {"request_id": "page-test", "owner_id": "owner", "event_log_id": event, "surface": "telegram"}
        response = client.post("/v1/initiative/evaluate", headers=_headers("page-test"), json=data)
        assert response.status_code == 200
        assert response.json()["created_count"] == 1
        assert len(requests) == 1
        assert len(pg.suggestions) == 1
        assert all(s["delivery_status"] == "not_attempted" for s in pg.suggestions.values())
        assert all(d["delivery_status"] == "not_attempted" for d in pg.initiative_decisions.values())
        # Replay both the same request and a fresh evaluation ID for the same event.
        for rid in ["page-test", "page-retry"]:
            data["request_id"] = rid
            assert client.post("/v1/initiative/evaluate", headers=_headers(rid), json=data).status_code == 200
        assert len(requests) == 1
    finally:
        client.close()


@pytest.mark.parametrize("denial", ["disabled", "owner", "event_owner", "prefs", "surface", "requested_surface", "threshold"])
def test_paging_denied(monkeypatch, denial):
    client, pg, _ = _client(monkeypatch)
    settings = _paging_settings()
    if denial == "disabled":
        settings.proactive_ntfy_enabled = False
    if denial == "owner":
        settings.proactive_ntfy_owner_id = "another-owner"
    monkeypatch.setattr(main_module, "settings", settings)
    requests = _mock_paging(monkeypatch, pg)
    try:
        if denial != "prefs":
            _enable(client, surfaces=["web"] if denial == "surface" else ["telegram"])
        event = _portfolio_event(pg, owner_id="other" if denial == "event_owner" else "owner", drift=0 if denial == "threshold" else 0.09)
        data = {"request_id": "denied", "owner_id": "owner", "event_log_id": event,
                "surface": "web" if denial == "requested_surface" else "telegram"}
        assert client.post("/v1/internal/proactive/evaluate", headers=_headers("denied"), json=data).status_code == 200
        assert requests == []
    finally:
        client.close()


@pytest.mark.asyncio
async def test_concurrent_evaluations_publish_only_insert_winner(monkeypatch):
    pg = FakePG()
    pg.prefs["owner"] = {"enabled": True, "allowed_surfaces_json": ["telegram"]}
    event = _portfolio_event(pg)
    requests = _mock_paging(monkeypatch, pg)
    original = pg.create_proactive_suggestion
    arrivals = 0
    ready = asyncio.Event()

    async def simultaneous_insert(**kwargs):
        nonlocal arrivals
        arrivals += 1
        if arrivals == 2:
            ready.set()
        await ready.wait()
        return await original(**kwargs)

    monkeypatch.setattr(pg, "create_proactive_suggestion", simultaneous_insert)
    async def evaluate(rid):
        return await proactive.evaluate_event(
            pg=pg, qdrant=FakeQdrant(), settings=_paging_settings(), request_id=rid,
            owner_id="owner", event_log_id=uuid.UUID(event), surface="telegram",
        )
    await asyncio.wait_for(asyncio.gather(evaluate("a"), evaluate("b")), timeout=2)
    assert len(pg.suggestions) == 1
    assert len(requests) == 1


def test_validation_candidate_does_not_publish(monkeypatch):
    pg = FakePG()
    event = _portfolio_event(pg)
    requests = _mock_paging(monkeypatch, pg)
    proactive.build_portfolio_suggestion_candidate(
        owner_id="owner", event_log=pg.event_logs[event], threshold=0.05,
        delivery_surface="telegram", generation_trace_id="validation",
    )
    assert requests == []
    assert pg.suggestions == {}


def test_paging_config_defaults_and_validation():
    from settings import Settings
    assert Settings().proactive_ntfy_enabled is False
    for overrides in [
        {}, {"PROACTIVE_NTFY_TOPIC": "alerts"}, {"PROACTIVE_NTFY_TOPIC": "a/b"},
        {"PROACTIVE_NTFY_BASE_URL": "https://ntfy.example/path"},
        {"PROACTIVE_NTFY_BASE_URL": "https://user:secret@ntfy.example"},
        {"PROACTIVE_NTFY_TIMEOUT_S": 11}, {"PROACTIVE_NTFY_OWNER_ID": ""},
    ]:
        config = {"PROACTIVE_NTFY_ENABLED": True}
        if overrides:
            config.update(PROACTIVE_NTFY_BASE_URL="https://ntfy.example", PROACTIVE_NTFY_TOPIC="ccp-pages", PROACTIVE_NTFY_OWNER_ID="owner")
            config.update(overrides)
        with pytest.raises(ValueError):
            Settings(**config)


@pytest.mark.parametrize("suppression", ["cooldown", "feedback"])
def test_paging_policy_suppression(monkeypatch, suppression):
    client, pg, _ = _client(monkeypatch)
    monkeypatch.setattr(main_module, "settings", _paging_settings())
    requests = _mock_paging(monkeypatch, pg)

    async def prior(**kwargs):
        return {"decision_id": "synthetic", "cooldown_until": None}

    monkeypatch.setattr(pg, "get_recent_initiative_cooldown" if suppression == "cooldown"
                        else "get_recent_negative_initiative_feedback", prior)
    try:
        _enable(client)
        event = _portfolio_event(pg)
        data = {"request_id": "suppressed", "owner_id": "owner", "event_log_id": event}
        response = client.post("/v1/initiative/evaluate", headers=_headers("suppressed"), json=data)
        assert response.status_code == 200
        assert response.json()["decisions"][0]["decision_status"] == "suppressed"
        assert requests == []
    finally:
        client.close()


@pytest.mark.asyncio
async def test_decision_persistence_failure_never_publishes(monkeypatch):
    pg = FakePG()
    pg.prefs["owner"] = {"enabled": True, "allowed_surfaces_json": ["telegram"]}
    event = _portfolio_event(pg)
    requests = _mock_paging(monkeypatch, pg)

    async def fail(**kwargs):
        raise RuntimeError("synthetic persistence failure")

    monkeypatch.setattr(pg, "create_initiative_decision", fail)
    with pytest.raises(RuntimeError, match="synthetic persistence"):
        await proactive.evaluate_event(
            pg=pg, qdrant=FakeQdrant(), settings=_paging_settings(), request_id="failure",
            owner_id="owner", event_log_id=uuid.UUID(event), surface="telegram",
        )
    assert len(pg.suggestions) == 1
    assert requests == []
