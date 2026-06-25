from __future__ import annotations

import os
import pytest


TEST_ENV_DEFAULTS = {
    "MEMORY_API_KEY": "testkey",
    "PG_DSN": "postgresql://test:test@127.0.0.1:1/test",
    "QDRANT_URL": "http://127.0.0.1:1",
    "LITELLM_BASE_URL": "http://127.0.0.1:1",
    "LITELLM_API_KEY": "testkey",
    "CHAT_MODEL": "test-chat",
    "EMBED_MODEL": "test-embed",
    "OBJECT_STORE_ENABLED": "false",
}

for name, value in TEST_ENV_DEFAULTS.items():
    os.environ.setdefault(name, value)


@pytest.fixture
def postgres_database() -> str:
    from pathlib import Path
    import subprocess
    import sys
    from uuid import uuid4

    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    admin = os.environ.get(
        "TEST_PG_DSN",
        "postgresql://memory_user:pass@127.0.0.1:15432/memory_db",
    )
    try:
        with psycopg.connect(admin) as conn:
            conn.execute("SELECT 1")
    except psycopg.Error:
        pytest.skip("PostgreSQL 16 test instance is not available")

    params = conninfo_to_dict(admin)
    base_dbname = params.get("dbname") or "postgres"
    dbname = f"bms_test_{uuid4().hex[:12]}"
    admin_params = {**params, "dbname": base_dbname}
    with psycopg.connect(make_conninfo(**admin_params), autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{dbname}"')
    dsn = make_conninfo(**{**params, "dbname": dbname})
    root = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "tools.schema_migrations",
            "upgrade",
            "--dsn",
            dsn,
            "--db-dir",
            str(root / "db"),
        ],
        cwd=root / "api",
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stdout + completed.stderr)
    try:
        yield dsn
    finally:
        with psycopg.connect(make_conninfo(**admin_params), autocommit=True) as conn:
            conn.execute(
                """
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = %s AND pid <> pg_backend_pid()
                """,
                (dbname,),
            )
            conn.execute(f'DROP DATABASE IF EXISTS "{dbname}"')
