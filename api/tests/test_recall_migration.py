from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_recall_decisions_migration_is_additive_and_indexed():
    migration = ROOT / "db" / "migrations" / "legacy" / "20260602_cluster9c_r22_recall_decisions_additive.sql"
    sql = migration.read_text()

    assert "CREATE TABLE IF NOT EXISTS recall_decisions" in sql
    assert "candidate_type TEXT NOT NULL CHECK" in sql
    assert "memory_item" in sql
    assert "derived_text" in sql
    assert "decision TEXT NOT NULL CHECK" in sql
    assert "mention_strategy TEXT NOT NULL CHECK" in sql
    assert "prompt_eligible BOOLEAN NOT NULL DEFAULT false" in sql
    assert "idx_recall_decisions_request_candidate" in sql
    assert "ON recall_decisions(request_id, owner_id, candidate_type, candidate_id)" in sql
    assert "idx_recall_decisions_request_debug" in sql
    assert "ON recall_decisions(request_id, owner_id, created_at ASC, id ASC)" in sql
