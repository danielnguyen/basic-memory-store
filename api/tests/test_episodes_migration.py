from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_cluster9b_migration_is_additive_and_has_episode_indexes():
    migration = ROOT / "db" / "migrations" / "legacy" / "20260601_cluster9b_r21_episodes_additive.sql"
    sql = migration.read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS episodes" in sql
    assert "CREATE TABLE IF NOT EXISTS episode_links" in sql
    assert "CREATE TABLE IF NOT EXISTS episode_events" in sql
    assert "idx_episodes_owner_episode_key_active" in sql
    assert "ON episodes(owner_id, episode_key)" in sql
    assert "WHERE status = 'active'" in sql
    assert "idx_episode_links_episode_ref_relationship" in sql
    assert "event_type IN ('created', 'updated', 'linked')" in sql
