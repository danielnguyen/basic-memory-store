from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from uuid import uuid4

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
import pytest
import yaml

from tools import schema_migrations


ROOT = Path(__file__).resolve().parents[2]
API_DIR = ROOT / "api"
SOURCE_DB_DIR = ROOT / "db"
RECENT_LEGACY_MIGRATIONS = [
    SOURCE_DB_DIR / "migrations" / "legacy" / "20260531_cluster9a_r20_memory_items_additive.sql",
    SOURCE_DB_DIR / "migrations" / "legacy" / "20260601_cluster9b_r21_episodes_additive.sql",
    SOURCE_DB_DIR / "migrations" / "legacy" / "20260602_cluster9c_r22_recall_decisions_additive.sql",
    SOURCE_DB_DIR / "migrations" / "legacy" / "20260603_cluster10a_r23_initiative_additive.sql",
]
RECENT_TABLES = [
    "initiative_feedback",
    "initiative_decisions",
    "initiative_events",
    "recall_decisions",
    "episode_events",
    "episode_links",
    "episodes",
    "memory_events",
    "memory_items",
]
ADVERSARIAL_BASELINE_SQL = """
CREATE TABLE IF NOT EXISTS comparator_parent (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  code TEXT NOT NULL UNIQUE,
  alt_code TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS comparator_child (
  owner_id TEXT NOT NULL,
  surface TEXT NOT NULL,
  client_id TEXT NOT NULL,
  parent_code TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  score INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (owner_id, surface, client_id),
  UNIQUE (surface, parent_code),
  CONSTRAINT comparator_child_parent_code_fk
    FOREIGN KEY (parent_code) REFERENCES comparator_parent(code) ON DELETE SET NULL,
  CHECK (score >= 0)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_comparator_child_status_created
  ON comparator_child(status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_comparator_child_parent_active
  ON comparator_child(parent_code, created_at DESC)
  WHERE status = 'active';
"""


def admin_dsn() -> str:
    return os.environ.get("TEST_PG_DSN", "postgresql://memory_user:pass@127.0.0.1:15432/memory_db")


def can_connect(dsn: str) -> bool:
    try:
        with psycopg.connect(dsn) as conn:
            conn.execute("SELECT 1")
        return True
    except psycopg.Error:
        return False

@pytest.fixture
def pg_database() -> str:
    admin = admin_dsn()
    if not can_connect(admin):
        pytest.skip("PostgreSQL 16 test instance is not available")
    params = conninfo_to_dict(admin)
    base_dbname = params.get("dbname") or "postgres"
    dbname = f"schema_test_{uuid4().hex[:12]}"
    with psycopg.connect(make_conninfo(**{**params, "dbname": base_dbname}), autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{dbname}"')
    try:
        yield make_conninfo(**{**params, "dbname": dbname})
    finally:
        with psycopg.connect(make_conninfo(**{**params, "dbname": base_dbname}), autocommit=True) as conn:
            conn.execute(
                """
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = %s AND pid <> pg_backend_pid()
                """,
                (dbname,),
            )
            conn.execute(f'DROP DATABASE IF EXISTS "{dbname}"')


@pytest.fixture
def temp_db_dir(tmp_path: Path) -> Path:
    db_dir = tmp_path / "db"
    (db_dir / "migrations" / "managed").mkdir(parents=True)
    (db_dir / "migrations" / "legacy").mkdir(parents=True)
    shutil.copy2(SOURCE_DB_DIR / "baseline.sql", db_dir / "baseline.sql")
    return db_dir


def run_cli(command: str, *, dsn: str, db_dir: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["BMS_DB_DIR"] = str(db_dir)
    return subprocess.run(
        [sys.executable, "-m", "tools.schema_migrations", command, "--dsn", dsn, "--db-dir", str(db_dir)],
        cwd=API_DIR,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def run_cli_without_dsn(*, command: str, db_dir: Path, env_overrides: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("PG_DSN", None)
    env["BMS_DB_DIR"] = str(db_dir)
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-m", "tools.schema_migrations", command, "--db-dir", str(db_dir)],
        cwd=API_DIR,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def launch_cli(command: str, *, dsn: str, db_dir: Path) -> subprocess.Popen[str]:
    env = os.environ.copy()
    env["BMS_DB_DIR"] = str(db_dir)
    return subprocess.Popen(
        [sys.executable, "-m", "tools.schema_migrations", command, "--dsn", dsn, "--db-dir", str(db_dir)],
        cwd=API_DIR,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )


def run_cli_ok(command: str, *, dsn: str, db_dir: Path) -> dict[str, object]:
    completed = run_cli(command, dsn=dsn, db_dir=db_dir)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return json.loads(completed.stdout.strip())


def run_cli_fail(command: str, *, dsn: str, db_dir: Path) -> dict[str, object]:
    completed = run_cli(command, dsn=dsn, db_dir=db_dir)
    assert completed.returncode != 0, completed.stdout + completed.stderr
    return json.loads(completed.stdout.strip())


def execute_sql(dsn: str, sql_text: str) -> None:
    with psycopg.connect(dsn) as conn:
        conn.execute(sql_text)
        conn.commit()


def query_value(dsn: str, query: str, params: tuple[object, ...] = ()) -> object:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(query, params).fetchone()
        return None if row is None else row[0]


def table_exists(dsn: str, table_name: str) -> bool:
    return bool(
        query_value(
            dsn,
            """
            SELECT EXISTS (
              SELECT 1
              FROM information_schema.tables
              WHERE table_schema = 'public' AND table_name = %s
            )
            """,
            (table_name,),
        )
    )


def column_exists(dsn: str, table_name: str, column_name: str) -> bool:
    return bool(
        query_value(
            dsn,
            """
            SELECT EXISTS (
              SELECT 1
              FROM information_schema.columns
              WHERE table_schema = 'public' AND table_name = %s AND column_name = %s
            )
            """,
            (table_name, column_name),
        )
    )


def ledger_rows(dsn: str) -> list[tuple[str, str]]:
    with psycopg.connect(dsn) as conn:
        if not table_exists(dsn, "schema_migrations"):
            return []
        return conn.execute(
            "SELECT version, kind FROM schema_migrations ORDER BY applied_at ASC, version ASC"
        ).fetchall()


def seed_baseline_without_ledger(dsn: str, db_dir: Path) -> None:
    execute_sql(dsn, (db_dir / "baseline.sql").read_text(encoding="utf-8"))


def seed_manual_recent_shape(dsn: str, db_dir: Path) -> None:
    seed_baseline_without_ledger(dsn, db_dir)
    execute_sql(
        dsn,
        "DROP TABLE IF EXISTS " + ", ".join(RECENT_TABLES) + " CASCADE;",
    )
    for path in RECENT_LEGACY_MIGRATIONS:
        execute_sql(dsn, path.read_text(encoding="utf-8"))


def write_managed_migration(db_dir: Path, filename: str, sql_text: str) -> Path:
    path = db_dir / "migrations" / "managed" / filename
    path.write_text(sql_text, encoding="utf-8")
    return path


def write_env_file(path: Path, dsn: str) -> None:
    path.write_text(f"PG_DSN={dsn}\n", encoding="utf-8")


def append_to_baseline(db_dir: Path, sql_text: str) -> None:
    baseline_path = db_dir / "baseline.sql"
    baseline_path.write_text(
        baseline_path.read_text(encoding="utf-8") + "\n" + sql_text.strip() + "\n",
        encoding="utf-8",
    )


def rename_first_constraint(dsn: str, table_name: str, contype: str, new_name: str) -> None:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            """
            SELECT conname
            FROM pg_constraint con
            JOIN pg_class cls ON cls.oid = con.conrelid
            JOIN pg_namespace nsp ON nsp.oid = cls.relnamespace
            WHERE nsp.nspname = 'public'
              AND cls.relname = %s
              AND con.contype = %s
            ORDER BY conname
            LIMIT 1
            """,
            (table_name, contype),
        ).fetchone()
        assert row is not None
        conn.execute(f'ALTER TABLE "{table_name}" RENAME CONSTRAINT "{row[0]}" TO "{new_name}"')
        conn.commit()


def drop_first_constraint(dsn: str, table_name: str, contype: str) -> None:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            """
            SELECT conname
            FROM pg_constraint con
            JOIN pg_class cls ON cls.oid = con.conrelid
            JOIN pg_namespace nsp ON nsp.oid = cls.relnamespace
            WHERE nsp.nspname = 'public'
              AND cls.relname = %s
              AND con.contype = %s
            ORDER BY conname
            LIMIT 1
            """,
            (table_name, contype),
        ).fetchone()
        assert row is not None
        conn.execute(f'ALTER TABLE "{table_name}" DROP CONSTRAINT "{row[0]}"')
        conn.commit()


def seed_adversarial_baseline_without_ledger(dsn: str, db_dir: Path) -> None:
    append_to_baseline(db_dir, ADVERSARIAL_BASELINE_SQL)
    seed_baseline_without_ledger(dsn, db_dir)


def test_explicit_dsn_wins_over_exported_pg_dsn(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    write_env_file(env_file, "postgresql://from-dotenv:dotenvpass@dotenv-host:5432/dotenv_db")
    monkeypatch.setenv("PG_DSN", "postgresql://from-env:envpass@env-host:5432/env_db")

    resolved = schema_migrations.resolve_dsn(
        "postgresql://from-flag:flagpass@flag-host:5432/flag_db",
        env_path=env_file,
    )

    assert resolved == "postgresql://from-flag:flagpass@flag-host:5432/flag_db"


def test_exported_pg_dsn_wins_over_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    write_env_file(env_file, "postgresql://from-dotenv:dotenvpass@dotenv-host:5432/dotenv_db")
    monkeypatch.setenv("PG_DSN", "postgresql://from-env:envpass@env-host:5432/env_db")

    resolved = schema_migrations.resolve_dsn(None, env_path=env_file)

    assert resolved == "postgresql://from-env:envpass@env-host:5432/env_db"


def test_dotenv_is_used_when_no_explicit_or_exported_dsn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env_file = tmp_path / ".env"
    write_env_file(env_file, "postgresql://from-dotenv:dotenvpass@dotenv-host:5432/dotenv_db")
    monkeypatch.delenv("PG_DSN", raising=False)

    resolved = schema_migrations.resolve_dsn(None, env_path=env_file)

    assert resolved == "postgresql://from-dotenv:dotenvpass@dotenv-host:5432/dotenv_db"
    assert os.environ["PG_DSN"] == "postgresql://from-dotenv:dotenvpass@dotenv-host:5432/dotenv_db"


def test_missing_dotenv_and_missing_pg_dsn_keep_safe_error(
    monkeypatch: pytest.MonkeyPatch, temp_db_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("PG_DSN", raising=False)
    monkeypatch.setattr(schema_migrations, "api_env_path", lambda: tmp_path / "missing.env")
    monkeypatch.setattr(
        sys,
        "argv",
        ["schema_migrations.py", "status", "--db-dir", str(temp_db_dir)],
    )

    exit_code = schema_migrations.main()
    captured = capsys.readouterr()

    assert exit_code == 2
    assert json.loads(captured.out.strip()) == {"error": "PG_DSN is required.", "ok": False}


@pytest.mark.parametrize(
    ("bad_dsn", "secret_parts"),
    [
        (
            "postgresql://demo:supersecret!@127.0.0.1:1/does_not_exist",
            ["demo", "supersecret!", "127.0.0.1"],
        ),
        (
            "host=127.0.0.1 port=1 dbname=does_not_exist user=demo password=s3cr3t!value",
            ["demo", "s3cr3t!value", "127.0.0.1"],
        ),
    ],
)
def test_status_and_errors_redact_uri_and_keyword_dsn_credentials(
    temp_db_dir: Path, bad_dsn: str, secret_parts: list[str]
) -> None:
    completed = run_cli("status", dsn=bad_dsn, db_dir=temp_db_dir)
    combined = completed.stdout + completed.stderr

    assert completed.returncode != 0
    assert bad_dsn not in combined
    for secret in secret_parts:
        assert secret not in combined


def test_empty_database_installs_baseline_and_records_it(pg_database: str, temp_db_dir: Path) -> None:
    payload = run_cli_ok("upgrade", dsn=pg_database, db_dir=temp_db_dir)

    assert payload["state"] == "current"
    assert payload["baseline_installed"] is True
    assert ledger_rows(pg_database) == [("schema_baseline_20260620", "baseline")]
    for table_name in (
        "memory_items",
        "memory_events",
        "episodes",
        "episode_links",
        "episode_events",
        "recall_decisions",
        "initiative_events",
        "initiative_decisions",
        "initiative_feedback",
    ):
        assert table_exists(pg_database, table_name)


def test_rerunning_upgrade_is_a_no_op(pg_database: str, temp_db_dir: Path) -> None:
    run_cli_ok("upgrade", dsn=pg_database, db_dir=temp_db_dir)
    second = run_cli_ok("upgrade", dsn=pg_database, db_dir=temp_db_dir)

    assert second["state"] == "current"
    assert second["baseline_installed"] is False
    assert second["applied_migrations"] == []
    assert ledger_rows(pg_database) == [("schema_baseline_20260620", "baseline")]


def test_check_passes_on_current_schema(pg_database: str, temp_db_dir: Path) -> None:
    run_cli_ok("upgrade", dsn=pg_database, db_dir=temp_db_dir)
    payload = run_cli_ok("check", dsn=pg_database, db_dir=temp_db_dir)
    assert payload["state"] == "current"


def test_upgrade_rejects_non_empty_database_without_ledger(pg_database: str, temp_db_dir: Path) -> None:
    execute_sql(pg_database, "CREATE TABLE scratchpad(id integer);")
    payload = run_cli_fail("upgrade", dsn=pg_database, db_dir=temp_db_dir)
    assert "adoption_required" in str(payload["error"])


def test_exact_existing_baseline_can_be_adopted(pg_database: str, temp_db_dir: Path) -> None:
    seed_baseline_without_ledger(pg_database, temp_db_dir)
    payload = run_cli_ok("adopt-baseline", dsn=pg_database, db_dir=temp_db_dir)
    assert payload["adopted_baseline"] is True
    assert ledger_rows(pg_database) == [("schema_baseline_20260620", "baseline")]


def test_manually_upgraded_shape_adopts_successfully(pg_database: str, temp_db_dir: Path) -> None:
    seed_manual_recent_shape(pg_database, temp_db_dir)
    payload = run_cli_ok("adopt-baseline", dsn=pg_database, db_dir=temp_db_dir)
    assert payload["adopted_baseline"] is True


@pytest.mark.parametrize(
    ("mutation_sql", "expected_fragment"),
    [
        ("DROP TABLE memory_items CASCADE;", "missing tables"),
        ("DROP TABLE recall_decisions CASCADE;", "missing tables"),
        ("DROP TABLE initiative_feedback CASCADE;", "missing tables"),
    ],
)
def test_missing_required_tables_fail_adoption(
    pg_database: str, temp_db_dir: Path, mutation_sql: str, expected_fragment: str
) -> None:
    seed_baseline_without_ledger(pg_database, temp_db_dir)
    execute_sql(pg_database, mutation_sql)
    payload = run_cli_fail("adopt-baseline", dsn=pg_database, db_dir=temp_db_dir)
    assert expected_fragment in str(payload["error"]).lower()


@pytest.mark.parametrize(
    "mutation_sql",
    [
        "ALTER TABLE messages ALTER COLUMN role TYPE VARCHAR(8);",
        "ALTER TABLE proactive_prefs ALTER COLUMN enabled DROP NOT NULL;",
        "ALTER TABLE initiative_events ALTER COLUMN trigger_ref_json SET DEFAULT '[]'::jsonb;",
    ],
)
def test_wrong_column_shape_fails_adoption(pg_database: str, temp_db_dir: Path, mutation_sql: str) -> None:
    seed_baseline_without_ledger(pg_database, temp_db_dir)
    execute_sql(pg_database, mutation_sql)
    payload = run_cli_fail("adopt-baseline", dsn=pg_database, db_dir=temp_db_dir)
    assert "column mismatch" in str(payload["error"]).lower()


def test_missing_foreign_key_fails_adoption(pg_database: str, temp_db_dir: Path) -> None:
    seed_baseline_without_ledger(pg_database, temp_db_dir)
    drop_first_constraint(pg_database, "messages", "f")
    payload = run_cli_fail("adopt-baseline", dsn=pg_database, db_dir=temp_db_dir)
    assert "foreign key mismatch" in str(payload["error"]).lower()

@pytest.mark.parametrize(
    ("mutation", "matcher"),
    [
        ("check", "check constraint mismatch"),
        ("DROP INDEX idx_messages_owner_time;", "index mismatch"),
    ],
)
def test_missing_check_or_index_fails_adoption(
    pg_database: str, temp_db_dir: Path, mutation: str, matcher: str
) -> None:
    seed_baseline_without_ledger(pg_database, temp_db_dir)
    if mutation == "check":
        drop_first_constraint(pg_database, "messages", "c")
    else:
        execute_sql(pg_database, mutation)
    payload = run_cli_fail("adopt-baseline", dsn=pg_database, db_dir=temp_db_dir)
    assert matcher in str(payload["error"]).lower()


def test_missing_unique_constraint_fails_adoption(pg_database: str, temp_db_dir: Path) -> None:
    seed_baseline_without_ledger(pg_database, temp_db_dir)
    drop_first_constraint(pg_database, "initiative_events", "u")
    payload = run_cli_fail("adopt-baseline", dsn=pg_database, db_dir=temp_db_dir)
    assert "unique constraint mismatch" in str(payload["error"]).lower()


def test_semantically_identical_constraints_with_different_names_are_accepted(
    pg_database: str, temp_db_dir: Path
) -> None:
    seed_baseline_without_ledger(pg_database, temp_db_dir)
    rename_first_constraint(pg_database, "messages", "f", "messages_conversation_fk_custom")
    payload = run_cli_ok("adopt-baseline", dsn=pg_database, db_dir=temp_db_dir)
    assert payload["adopted_baseline"] is True


def test_schema_qualified_defaults_and_whitespace_equivalence_are_accepted(
    pg_database: str, temp_db_dir: Path
) -> None:
    seed_baseline_without_ledger(pg_database, temp_db_dir)
    execute_sql(
        pg_database,
        """
        ALTER TABLE messages ALTER COLUMN created_at SET DEFAULT pg_catalog.now();
        ALTER TABLE proactive_prefs ALTER COLUMN allowed_surfaces_json SET DEFAULT ( '[]' :: jsonb );
        """,
    )

    payload = run_cli_ok("adopt-baseline", dsn=pg_database, db_dir=temp_db_dir)

    assert payload["adopted_baseline"] is True


def test_unexpected_application_owned_table_fails_adoption(pg_database: str, temp_db_dir: Path) -> None:
    seed_baseline_without_ledger(pg_database, temp_db_dir)
    execute_sql(pg_database, "CREATE TABLE rogue_application_table(id integer);")

    payload = run_cli_fail("adopt-baseline", dsn=pg_database, db_dir=temp_db_dir)

    assert "unexpected tables" in str(payload["error"]).lower()


def test_composite_primary_key_mismatch_fails_adoption(pg_database: str, temp_db_dir: Path) -> None:
    seed_baseline_without_ledger(pg_database, temp_db_dir)
    execute_sql(
        pg_database,
        """
        ALTER TABLE surface_profile_defaults DROP CONSTRAINT surface_profile_defaults_pkey;
        ALTER TABLE surface_profile_defaults ADD PRIMARY KEY (owner_id, client_id, surface);
        """,
    )

    payload = run_cli_fail("adopt-baseline", dsn=pg_database, db_dir=temp_db_dir)

    assert "primary key mismatch" in str(payload["error"]).lower()


def test_unique_standalone_index_with_equivalent_definition_is_accepted(
    pg_database: str, temp_db_dir: Path
) -> None:
    seed_adversarial_baseline_without_ledger(pg_database, temp_db_dir)
    execute_sql(
        pg_database,
        """
        DROP INDEX idx_comparator_child_status_created;
        CREATE UNIQUE INDEX comparator_child_status_created_alt
          ON comparator_child ( status , created_at DESC );
        """,
    )

    payload = run_cli_ok("adopt-baseline", dsn=pg_database, db_dir=temp_db_dir)

    assert payload["adopted_baseline"] is True


def test_changed_partial_index_predicate_fails_adoption(pg_database: str, temp_db_dir: Path) -> None:
    seed_adversarial_baseline_without_ledger(pg_database, temp_db_dir)
    execute_sql(
        pg_database,
        """
        DROP INDEX idx_comparator_child_parent_active;
        CREATE INDEX idx_comparator_child_parent_active
          ON comparator_child(parent_code, created_at DESC)
          WHERE status = 'inactive';
        """,
    )

    payload = run_cli_fail("adopt-baseline", dsn=pg_database, db_dir=temp_db_dir)

    assert "index mismatch" in str(payload["error"]).lower()


def test_index_sort_direction_mismatch_fails_adoption(pg_database: str, temp_db_dir: Path) -> None:
    seed_baseline_without_ledger(pg_database, temp_db_dir)
    execute_sql(
        pg_database,
        """
        DROP INDEX idx_messages_owner_time;
        CREATE INDEX idx_messages_owner_time ON messages(owner_id, created_at ASC);
        """,
    )

    payload = run_cli_fail("adopt-baseline", dsn=pg_database, db_dir=temp_db_dir)

    assert "index mismatch" in str(payload["error"]).lower()


def test_foreign_key_referenced_columns_and_on_delete_are_validated(
    pg_database: str, temp_db_dir: Path
) -> None:
    seed_adversarial_baseline_without_ledger(pg_database, temp_db_dir)
    execute_sql(
        pg_database,
        """
        ALTER TABLE comparator_child DROP CONSTRAINT comparator_child_parent_code_fk;
        ALTER TABLE comparator_child
          ADD CONSTRAINT comparator_child_parent_code_fk
          FOREIGN KEY (parent_code) REFERENCES comparator_parent(alt_code) ON DELETE CASCADE;
        """,
    )

    payload = run_cli_fail("adopt-baseline", dsn=pg_database, db_dir=temp_db_dir)

    assert "foreign key mismatch" in str(payload["error"]).lower()


def test_failed_adoption_leaves_no_ledger(pg_database: str, temp_db_dir: Path) -> None:
    seed_baseline_without_ledger(pg_database, temp_db_dir)
    execute_sql(pg_database, "DROP TABLE memory_items CASCADE;")
    run_cli_fail("adopt-baseline", dsn=pg_database, db_dir=temp_db_dir)
    assert not table_exists(pg_database, "schema_migrations")


def test_managed_migration_applies_once_and_commits_with_ledger(pg_database: str, temp_db_dir: Path) -> None:
    write_managed_migration(
        temp_db_dir,
        "20260620010101_memory_add_notes.sql",
        "ALTER TABLE memory_items ADD COLUMN notes TEXT;",
    )
    run_cli_ok("upgrade", dsn=pg_database, db_dir=temp_db_dir)
    second = run_cli_ok("upgrade", dsn=pg_database, db_dir=temp_db_dir)

    assert column_exists(pg_database, "memory_items", "notes")
    assert ("20260620010101", "migration") in ledger_rows(pg_database)
    assert second["applied_migrations"] == []


def test_failed_managed_migration_rolls_back_and_remains_pending(pg_database: str, temp_db_dir: Path) -> None:
    write_managed_migration(
        temp_db_dir,
        "20260620010101_memory_add_notes.sql",
        "ALTER TABLE memory_items ADD COLUMN notes TEXT; SELECT * FROM definitely_missing_table;",
    )
    payload = run_cli_fail("upgrade", dsn=pg_database, db_dir=temp_db_dir)

    assert "definitely_missing_table" in str(payload["error"])
    assert not column_exists(pg_database, "memory_items", "notes")
    assert ("20260620010101", "migration") not in ledger_rows(pg_database)
    status = run_cli_ok("status", dsn=pg_database, db_dir=temp_db_dir)
    assert status["state"] == "pending"
    assert status["pending_migrations"] == ["20260620010101_memory_add_notes.sql"]


def test_altered_applied_migration_checksum_fails(pg_database: str, temp_db_dir: Path) -> None:
    path = write_managed_migration(
        temp_db_dir,
        "20260620010101_memory_add_notes.sql",
        "ALTER TABLE memory_items ADD COLUMN notes TEXT;",
    )
    run_cli_ok("upgrade", dsn=pg_database, db_dir=temp_db_dir)
    path.write_text("ALTER TABLE memory_items ADD COLUMN notes TEXT;\n-- changed\n", encoding="utf-8")

    payload = run_cli_fail("check", dsn=pg_database, db_dir=temp_db_dir)
    assert "checksum" in str(payload["error"]).lower()


def test_altered_baseline_checksum_fails(pg_database: str, temp_db_dir: Path) -> None:
    run_cli_ok("upgrade", dsn=pg_database, db_dir=temp_db_dir)
    baseline = temp_db_dir / "baseline.sql"
    baseline.write_text(baseline.read_text(encoding="utf-8") + "\n-- changed\n", encoding="utf-8")

    payload = run_cli_fail("check", dsn=pg_database, db_dir=temp_db_dir)
    assert "baseline checksum" in str(payload["error"]).lower()


@pytest.mark.parametrize("mode", ["missing_file", "rogue_ledger_row"])
def test_unknown_or_missing_migration_ledger_entries_fail(
    pg_database: str, temp_db_dir: Path, mode: str
) -> None:
    path = write_managed_migration(
        temp_db_dir,
        "20260620010101_memory_add_notes.sql",
        "ALTER TABLE memory_items ADD COLUMN notes TEXT;",
    )
    run_cli_ok("upgrade", dsn=pg_database, db_dir=temp_db_dir)
    if mode == "missing_file":
        path.unlink()
    else:
        execute_sql(
            pg_database,
            """
            INSERT INTO schema_migrations (version, kind, checksum_sha256, execution_ms)
            VALUES ('20990101010101', 'migration', repeat('0', 64), 1)
            """,
        )

    payload = run_cli_fail("check", dsn=pg_database, db_dir=temp_db_dir)
    assert "no corresponding managed file" in str(payload["error"]).lower()


def test_concurrent_runners_apply_migration_only_once(pg_database: str, temp_db_dir: Path) -> None:
    write_managed_migration(
        temp_db_dir,
        "20260620010101_memory_add_notes.sql",
        "SELECT pg_sleep(2); ALTER TABLE memory_items ADD COLUMN notes TEXT;",
    )
    one = launch_cli("upgrade", dsn=pg_database, db_dir=temp_db_dir)
    two = launch_cli("upgrade", dsn=pg_database, db_dir=temp_db_dir)
    one_stdout, one_stderr = one.communicate(timeout=60)
    two_stdout, two_stderr = two.communicate(timeout=60)

    assert one.returncode == 0, one_stdout + one_stderr
    assert two.returncode == 0, two_stdout + two_stderr
    assert ledger_rows(pg_database).count(("20260620010101", "migration")) == 1
    assert column_exists(pg_database, "memory_items", "notes")


def test_status_and_errors_never_expose_dsn_password(temp_db_dir: Path) -> None:
    bad_dsn = "postgresql://demo:supersecret@127.0.0.1:1/does_not_exist"
    completed = run_cli("status", dsn=bad_dsn, db_dir=temp_db_dir)
    combined = completed.stdout + completed.stderr

    assert completed.returncode != 0
    assert "supersecret" not in combined
    assert bad_dsn not in combined


def test_compose_configuration_has_migration_dependency_ordering() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]

    migrate = services["memory-db-migrate"]
    assert migrate["command"] == ["python", "-m", "tools.schema_migrations", "upgrade"]
    assert list(migrate["environment"]) == [
        "PG_DSN=postgresql://memory_user:${POSTGRES_PASSWORD}@memory-db-postgres:5432/memory_db"
    ]
    assert services["basic-memory-store"]["depends_on"]["memory-db-migrate"]["condition"] == "service_completed_successfully"
