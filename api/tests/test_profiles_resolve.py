import types

from fastapi.testclient import TestClient

import main as main_module


class FakePG:
    async def open(self):
        return None

    async def close(self):
        return None

    async def ping(self):
        return True

    async def resolve_profile(self, **kwargs):
        return {
            "profile_name": "dev",
            "source": "requested",
            "profile_version": 3,
            "effective_profile_ref": "owner:dev:3",
            "prompt_overlay": "You are concise",
            "retrieval_policy": {"k": 6},
            "routing_policy": {"cost_mode": "balanced"},
            "response_style": {"verbosity": "low"},
            "safety_policy": {},
            "tool_policy": {},
        }


class FakeQdrant:
    def ping(self):
        return True


def test_profiles_resolve_returns_effective_ref(monkeypatch):
    fake_settings = types.SimpleNamespace(
        memory_api_key="testkey",
        enable_profile_resolve=True,
        default_profile_name="dev",
    )
    monkeypatch.setattr(main_module, "settings", fake_settings, raising=True)
    monkeypatch.setattr(main_module, "pg", FakePG(), raising=True)
    monkeypatch.setattr(main_module, "qdrant", FakeQdrant(), raising=True)

    client = TestClient(main_module.app)
    try:
        r = client.post(
            "/v1/profiles/resolve",
            headers={"X-API-Key": "testkey"},
            json={"owner_id": "owner", "surface": "vscode", "requested_profile": "dev", "client_id": ""},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["effective_profile_ref"] == "owner:dev:3"
    finally:
        client.close()
