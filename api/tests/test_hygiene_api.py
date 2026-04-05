import types
import uuid

from fastapi.testclient import TestClient

import main as main_module


class FakePG:
    def __init__(self):
        self.flags = []
        self.rows = [
            {
                "id": "00000000-0000-0000-0000-000000000001",
                "conversation_id": None,
                "content": "Daniel prefers concise summaries",
                "metadata": {"topic": "summary_style", "value": "concise"},
                "created_at": "2026-01-01T00:00:00+00:00",
            },
            {
                "id": "00000000-0000-0000-0000-000000000002",
                "conversation_id": None,
                "content": "Daniel prefers concise summaries",
                "metadata": {"topic": "summary_style", "value": "concise"},
                "created_at": "2026-01-02T00:00:00+00:00",
            },
            {
                "id": "00000000-0000-0000-0000-000000000003",
                "conversation_id": None,
                "content": "Daniel prefers detailed summaries",
                "metadata": {"topic": "summary_style", "value": "detailed"},
                "created_at": "2026-01-03T00:00:00+00:00",
            },
        ]

    async def open(self):
        return None

    async def close(self):
        return None

    async def ping(self):
        return True

    async def get_pinned_memories_for_hygiene(self, owner_id: str, limit: int = 50):
        return list(self.rows)

    async def create_hygiene_flag(self, *, owner_id: str, subject_type: str, subject_id, flag_type: str, details=None):
        for row in self.flags:
            if (
                row["owner_id"] == owner_id
                and row["subject_type"] == subject_type
                and row["subject_id"] == (str(subject_id) if subject_id else None)
                and row["flag_type"] == flag_type
                and row["details"] == (details or {})
                and row["status"] == "open"
            ):
                return {**row, "created": False}
        row = {
            "flag_id": str(uuid.uuid4()),
            "owner_id": owner_id,
            "subject_type": subject_type,
            "subject_id": str(subject_id) if subject_id else None,
            "flag_type": flag_type,
            "details": details or {},
            "status": "open",
            "created_at": "2026-01-05T00:00:00+00:00",
            "resolved_at": None,
            "created": True,
        }
        self.flags.append(row)
        return row

    async def list_hygiene_flags(self, *, owner_id: str, status: str | None = None, limit: int = 50):
        rows = [row for row in self.flags if row["owner_id"] == owner_id]
        if status is not None:
            rows = [row for row in rows if row["status"] == status]
        return rows[:limit]


def test_hygiene_scan_and_list(monkeypatch):
    fake_settings = types.SimpleNamespace(
        memory_api_key="testkey",
        enable_hygiene_scan_api=True,
    )
    monkeypatch.setattr(main_module, "settings", fake_settings, raising=True)
    monkeypatch.setattr(main_module, "pg", FakePG(), raising=True)

    client = TestClient(main_module.app)
    try:
        scan = client.post(
            "/v1/hygiene/scan",
            headers={"X-API-Key": "testkey"},
            json={"owner_id": "owner"},
        )
        assert scan.status_code == 200
        body = scan.json()
        assert body["flags_created"] == 4
        assert {item["flag_type"] for item in body["flags"]} == {"pinned_redundancy", "pinned_contradiction"}

        scan_again = client.post(
            "/v1/hygiene/scan",
            headers={"X-API-Key": "testkey"},
            json={"owner_id": "owner"},
        )
        assert scan_again.status_code == 200
        assert scan_again.json()["flags_created"] == 0

        listed = client.get(
            "/v1/hygiene/flags",
            headers={"X-API-Key": "testkey"},
            params={"owner_id": "owner"},
        )
        assert listed.status_code == 200
        assert len(listed.json()["flags"]) == 4
    finally:
        client.close()
