from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_cluster9a_migration_is_additive_and_has_active_idempotency_index():
    migration = ROOT / "db" / "migrations" / "20260531_cluster9a_r20_memory_items_additive.sql"
    sql = migration.read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS memory_items" in sql
    assert "CREATE TABLE IF NOT EXISTS memory_events" in sql
    assert "idx_memory_items_owner_source_hash_active" in sql
    assert "ON memory_items(owner_id, source_ref_hash)" in sql
    assert "WHERE status = 'active'" in sql
    assert "supersedes_memory_id" in sql
    assert "superseded_by_memory_id" in sql
