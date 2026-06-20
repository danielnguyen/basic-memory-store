from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import psycopg
from psycopg import sql
from psycopg.rows import dict_row


BASELINE_VERSION = "schema_baseline_20260620"
LEDGER_TABLE = "schema_migrations"
LOCK_KEY = 612361446343624483
LOCK_TIMEOUT_SECONDS = 30.0
LOCK_RETRY_SECONDS = 0.5
EXPECTED_EXTENSIONS = {"pgcrypto"}
MANAGED_FILENAME_RE = re.compile(r"^(?P<version>\d{14})_(?P<name>[a-z0-9]+(?:_[a-z0-9]+)*)\.sql$")
REDACT_URI_RE = re.compile(r"(postgres(?:ql)?://)([^:@/]+)(?::([^@/]*))?@")
REDACT_PASSWORD_KV_RE = re.compile(r"(password=)([^ \t]+)", re.IGNORECASE)
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class MigrationError(RuntimeError):
    """Raised when migration lifecycle checks fail."""


@dataclass(frozen=True)
class ManagedMigration:
    version: str
    path: Path
    checksum_sha256: str

    @property
    def name(self) -> str:
        return self.path.name


def compute_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def find_db_dir(explicit: str | None = None) -> Path:
    env_override = explicit or os.environ.get("BMS_DB_DIR")
    candidates: list[Path] = []
    if env_override:
        candidates.append(Path(env_override).expanduser().resolve())

    here = Path(__file__).resolve()
    cwd = Path.cwd().resolve()
    for root in (cwd, *cwd.parents, here.parent, *here.parents):
        candidates.append(root)
        candidates.append(root / "db")

    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        db_dir = resolved if resolved.name == "db" else resolved / "db"
        if (db_dir / "baseline.sql").is_file() and (db_dir / "migrations").is_dir():
            return db_dir
    raise MigrationError("Unable to locate the db directory for baseline and migrations.")


def load_managed_migrations(db_dir: Path) -> list[ManagedMigration]:
    managed_dir = db_dir / "migrations" / "managed"
    if not managed_dir.is_dir():
        raise MigrationError(f"Managed migration directory is missing: {managed_dir}")

    migrations: list[ManagedMigration] = []
    versions: dict[str, Path] = {}
    malformed: list[str] = []
    for path in sorted(managed_dir.glob("*.sql")):
        match = MANAGED_FILENAME_RE.match(path.name)
        if not match:
            malformed.append(path.name)
            continue
        version = match.group("version")
        if version in versions:
            raise MigrationError(
                f"Duplicate managed migration version {version} in {versions[version].name} and {path.name}."
            )
        versions[version] = path
        migrations.append(ManagedMigration(version=version, path=path, checksum_sha256=compute_sha256(path)))

    if malformed:
        raise MigrationError(
            "Malformed managed migration filenames: " + ", ".join(sorted(malformed))
        )
    return migrations


def redact_dsn(value: str, dsn: str | None) -> str:
    redacted = value
    if dsn:
        redacted = redacted.replace(dsn, "<redacted-dsn>")
    redacted = REDACT_URI_RE.sub(r"\1<redacted>@", redacted)
    redacted = REDACT_PASSWORD_KV_RE.sub(r"\1<redacted>", redacted)
    return redacted


def normalize_sql_text(value: str, schema_names: Iterable[str] = ()) -> str:
    normalized = value.strip()
    normalized = normalized.replace('"', "")
    for schema_name in schema_names:
        normalized = re.sub(rf"\b{re.escape(schema_name)}\.", "", normalized)
    normalized = normalized.replace("public.", "")
    normalized = re.sub(r"\s+", " ", normalized)
    normalized = re.sub(r"\(\s+", "(", normalized)
    normalized = re.sub(r"\s+\)", ")", normalized)
    normalized = re.sub(r"\s*,\s*", ", ", normalized)
    return normalized.strip().lower()


def sanitize_exception(exc: Exception, dsn: str | None) -> str:
    return redact_dsn(str(exc), dsn)


def print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True))


def ledger_exists(conn: psycopg.Connection[Any]) -> bool:
    row = conn.execute(
        """
        SELECT EXISTS (
          SELECT 1
          FROM information_schema.tables
          WHERE table_schema = 'public' AND table_name = %s
        ) AS exists
        """,
        (LEDGER_TABLE,),
    ).fetchone()
    return bool(row["exists"])


def create_ledger_table(conn: psycopg.Connection[Any]) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {LEDGER_TABLE} (
          version TEXT PRIMARY KEY,
          kind TEXT NOT NULL CHECK (kind IN ('baseline', 'migration')),
          checksum_sha256 CHAR(64) NOT NULL,
          execution_ms INTEGER NOT NULL,
          applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def public_application_tables(conn: psycopg.Connection[Any]) -> list[str]:
    rows = conn.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_type = 'BASE TABLE'
          AND table_name <> %s
        ORDER BY table_name
        """,
        (LEDGER_TABLE,),
    ).fetchall()
    return [row["table_name"] for row in rows]


def read_ledger_rows(conn: psycopg.Connection[Any]) -> list[dict[str, Any]]:
    if not ledger_exists(conn):
        return []
    return conn.execute(
        f"""
        SELECT version, kind, checksum_sha256, execution_ms, applied_at
        FROM {LEDGER_TABLE}
        ORDER BY applied_at ASC, version ASC
        """
    ).fetchall()


def collect_schema_metadata(conn: psycopg.Connection[Any], schema_name: str) -> dict[str, Any]:
    schema_names = {schema_name, "public"}
    metadata: dict[str, Any] = {
        "extensions": set(),
        "tables": {},
        "columns": {},
        "primary_keys": {},
        "unique_constraints": {},
        "foreign_keys": {},
        "check_constraints": {},
        "indexes": {},
    }

    ext_rows = conn.execute(
        "SELECT extname FROM pg_extension WHERE extname = ANY(%s) ORDER BY extname",
        (list(EXPECTED_EXTENSIONS),),
    ).fetchall()
    metadata["extensions"] = {row["extname"] for row in ext_rows}

    table_rows = conn.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = %s
          AND table_type = 'BASE TABLE'
          AND table_name <> %s
        ORDER BY table_name
        """,
        (schema_name, LEDGER_TABLE),
    ).fetchall()
    metadata["tables"] = {row["table_name"] for row in table_rows}

    column_rows = conn.execute(
        """
        SELECT
          c.table_name,
          c.column_name,
          pg_catalog.format_type(a.atttypid, a.atttypmod) AS formatted_type,
          c.is_nullable,
          COALESCE(pg_get_expr(ad.adbin, ad.adrelid), '') AS default_expr
        FROM information_schema.columns c
        JOIN pg_namespace n
          ON n.nspname = c.table_schema
        JOIN pg_class cls
          ON cls.relname = c.table_name
         AND cls.relnamespace = n.oid
        JOIN pg_attribute a
          ON a.attrelid = cls.oid
         AND a.attname = c.column_name
        LEFT JOIN pg_attrdef ad
          ON ad.adrelid = cls.oid
         AND ad.adnum = a.attnum
        WHERE c.table_schema = %s
          AND c.table_name <> %s
        ORDER BY c.table_name, c.ordinal_position
        """,
        (schema_name, LEDGER_TABLE),
    ).fetchall()
    columns: dict[str, dict[str, dict[str, Any]]] = {}
    for row in column_rows:
        columns.setdefault(row["table_name"], {})[row["column_name"]] = {
            "type": normalize_sql_text(row["formatted_type"], schema_names),
            "nullable": row["is_nullable"] == "YES",
            "default": normalize_sql_text(row["default_expr"], schema_names) if row["default_expr"] else "",
        }
    metadata["columns"] = columns

    pk_rows = conn.execute(
        """
        SELECT
          cls.relname AS table_name,
          array_agg(att.attname ORDER BY ord.ordinality) AS columns
        FROM pg_constraint con
        JOIN pg_class cls ON cls.oid = con.conrelid
        JOIN pg_namespace nsp ON nsp.oid = cls.relnamespace
        JOIN unnest(con.conkey) WITH ORDINALITY AS ord(attnum, ordinality) ON TRUE
        JOIN pg_attribute att ON att.attrelid = cls.oid AND att.attnum = ord.attnum
        WHERE nsp.nspname = %s
          AND con.contype = 'p'
          AND cls.relname <> %s
        GROUP BY cls.relname
        ORDER BY cls.relname
        """,
        (schema_name, LEDGER_TABLE),
    ).fetchall()
    metadata["primary_keys"] = {row["table_name"]: list(row["columns"]) for row in pk_rows}

    unique_rows = conn.execute(
        """
        SELECT
          cls.relname AS table_name,
          pg_get_constraintdef(con.oid, false) AS definition
        FROM pg_constraint con
        JOIN pg_class cls ON cls.oid = con.conrelid
        JOIN pg_namespace nsp ON nsp.oid = cls.relnamespace
        WHERE nsp.nspname = %s
          AND con.contype = 'u'
          AND cls.relname <> %s
        ORDER BY cls.relname, con.oid
        """,
        (schema_name, LEDGER_TABLE),
    ).fetchall()
    unique_constraints: dict[str, list[str]] = {}
    for row in unique_rows:
        unique_constraints.setdefault(row["table_name"], []).append(
            normalize_sql_text(row["definition"], schema_names)
        )
    metadata["unique_constraints"] = {
        key: sorted(values) for key, values in unique_constraints.items()
    }

    fk_rows = conn.execute(
        """
        SELECT
          cls.relname AS table_name,
          pg_get_constraintdef(con.oid, false) AS definition
        FROM pg_constraint con
        JOIN pg_class cls ON cls.oid = con.conrelid
        JOIN pg_namespace nsp ON nsp.oid = cls.relnamespace
        WHERE nsp.nspname = %s
          AND con.contype = 'f'
          AND cls.relname <> %s
        ORDER BY cls.relname, con.oid
        """,
        (schema_name, LEDGER_TABLE),
    ).fetchall()
    foreign_keys: dict[str, list[str]] = {}
    for row in fk_rows:
        foreign_keys.setdefault(row["table_name"], []).append(
            normalize_sql_text(row["definition"], schema_names)
        )
    metadata["foreign_keys"] = {
        key: sorted(values) for key, values in foreign_keys.items()
    }

    check_rows = conn.execute(
        """
        SELECT
          cls.relname AS table_name,
          pg_get_constraintdef(con.oid, false) AS definition
        FROM pg_constraint con
        JOIN pg_class cls ON cls.oid = con.conrelid
        JOIN pg_namespace nsp ON nsp.oid = cls.relnamespace
        WHERE nsp.nspname = %s
          AND con.contype = 'c'
          AND cls.relname <> %s
        ORDER BY cls.relname, con.oid
        """,
        (schema_name, LEDGER_TABLE),
    ).fetchall()
    checks: dict[str, list[str]] = {}
    for row in check_rows:
        checks.setdefault(row["table_name"], []).append(
            normalize_sql_text(row["definition"], schema_names)
        )
    metadata["check_constraints"] = {key: sorted(values) for key, values in checks.items()}

    index_rows = conn.execute(
        """
        SELECT
          tbl.relname AS table_name,
          pg_get_indexdef(idx.indexrelid) AS definition
        FROM pg_index idx
        JOIN pg_class tbl ON tbl.oid = idx.indrelid
        JOIN pg_namespace nsp ON nsp.oid = tbl.relnamespace
        WHERE nsp.nspname = %s
          AND tbl.relname <> %s
          AND NOT EXISTS (
            SELECT 1
            FROM pg_constraint con
            WHERE con.conindid = idx.indexrelid
          )
        ORDER BY tbl.relname, idx.indexrelid
        """,
        (schema_name, LEDGER_TABLE),
    ).fetchall()
    indexes: dict[str, list[str]] = {}
    for row in index_rows:
        definition = normalize_sql_text(row["definition"], schema_names)
        definition = re.sub(
            rf"^create(?: unique)? index [^ ]+ on {re.escape(row['table_name'].lower())} ",
            "",
            definition,
        )
        indexes.setdefault(row["table_name"], []).append(definition)
    metadata["indexes"] = {key: sorted(values) for key, values in indexes.items()}

    return metadata


def compare_metadata(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    errors: list[str] = []

    if actual["extensions"] != expected["extensions"]:
        errors.append(
            "Required extensions mismatch: "
            f"expected {sorted(expected['extensions'])}, got {sorted(actual['extensions'])}"
        )

    if actual["tables"] != expected["tables"]:
        missing = sorted(expected["tables"] - actual["tables"])
        extra = sorted(actual["tables"] - expected["tables"])
        if missing:
            errors.append("Missing tables: " + ", ".join(missing))
        if extra:
            errors.append("Unexpected tables: " + ", ".join(extra))
        return errors

    for table in sorted(expected["tables"]):
        if actual["columns"].get(table, {}) != expected["columns"].get(table, {}):
            errors.append(f"Column mismatch in table {table}")
        if actual["primary_keys"].get(table, []) != expected["primary_keys"].get(table, []):
            errors.append(f"Primary key mismatch in table {table}")
        if actual["unique_constraints"].get(table, []) != expected["unique_constraints"].get(table, []):
            errors.append(f"Unique constraint mismatch in table {table}")
        if actual["foreign_keys"].get(table, []) != expected["foreign_keys"].get(table, []):
            errors.append(f"Foreign key mismatch in table {table}")
        if actual["check_constraints"].get(table, []) != expected["check_constraints"].get(table, []):
            errors.append(f"Check constraint mismatch in table {table}")
        if actual["indexes"].get(table, []) != expected["indexes"].get(table, []):
            errors.append(f"Index mismatch in table {table}")
    return errors


def execute_sql_file(conn: psycopg.Connection[Any], path: Path) -> int:
    sql_text = path.read_text(encoding="utf-8")
    if re.search(r"\bconcurrently\b", sql_text, re.IGNORECASE):
        raise MigrationError(
            f"Managed migration {path.name} uses unsupported non-transactional SQL such as CONCURRENTLY."
        )
    started = time.perf_counter()
    conn.execute(sql_text)
    return int((time.perf_counter() - started) * 1000)


def insert_ledger_row(
    conn: psycopg.Connection[Any],
    *,
    version: str,
    kind: str,
    checksum_sha256: str,
    execution_ms: int,
) -> None:
    conn.execute(
        f"""
        INSERT INTO {LEDGER_TABLE} (version, kind, checksum_sha256, execution_ms)
        VALUES (%s, %s, %s, %s)
        """,
        (version, kind, checksum_sha256, execution_ms),
    )


def create_reference_schema_name() -> str:
    suffix = "".join(random.choice("0123456789abcdef") for _ in range(12))
    name = f"schema_reference_{suffix}"
    if not IDENTIFIER_RE.match(name):
        raise MigrationError("Generated invalid reference schema name.")
    return name


def validate_schema_against_baseline(
    conn: psycopg.Connection[Any], *, baseline_path: Path, target_schema: str
) -> list[str]:
    reference_schema = create_reference_schema_name()
    conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(reference_schema)))
    try:
        conn.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(reference_schema)))
        execute_sql_file(conn, baseline_path)
        expected = collect_schema_metadata(conn, reference_schema)
        actual = collect_schema_metadata(conn, target_schema)
        return compare_metadata(expected, actual)
    finally:
        conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(reference_schema)))


def acquire_lock(conn: psycopg.Connection[Any]) -> None:
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        acquired = conn.execute(
            "SELECT pg_try_advisory_lock(%s) AS acquired",
            (LOCK_KEY,),
        ).fetchone()["acquired"]
        if acquired:
            return
        time.sleep(LOCK_RETRY_SECONDS)
    raise MigrationError("Could not acquire the schema migration advisory lock within 30 seconds.")


def release_lock(conn: psycopg.Connection[Any]) -> None:
    conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))


def analyze_state(conn: psycopg.Connection[Any], db_dir: Path) -> dict[str, Any]:
    baseline_path = db_dir / "baseline.sql"
    managed = load_managed_migrations(db_dir)
    baseline_checksum = compute_sha256(baseline_path)
    ledger_rows = read_ledger_rows(conn)
    application_tables = public_application_tables(conn)
    errors: list[str] = []
    baseline_status = "missing"
    latest_applied_migration: str | None = None
    pending_names: list[str] = []

    if not ledger_rows:
        if application_tables:
            state = "adoption_required"
        else:
            state = "empty"
        pending_names = [migration.name for migration in managed]
        return {
            "state": state,
            "baseline_version": BASELINE_VERSION,
            "baseline_checksum_status": baseline_status,
            "latest_applied_managed_migration": latest_applied_migration,
            "pending_migrations": pending_names,
            "pending_migration_count": len(pending_names),
            "errors": errors,
        }

    baseline_rows = [row for row in ledger_rows if row["kind"] == "baseline"]
    migration_rows = [row for row in ledger_rows if row["kind"] == "migration"]
    unknown_rows = [row for row in ledger_rows if row["kind"] not in {"baseline", "migration"}]
    if unknown_rows:
        errors.append("Ledger contains unknown row kinds.")

    if len(baseline_rows) != 1:
        errors.append("Ledger must contain exactly one baseline row.")
    else:
        baseline_row = baseline_rows[0]
        if baseline_row["version"] != BASELINE_VERSION:
            errors.append(f"Ledger baseline version {baseline_row['version']} is not recognized.")
        elif baseline_row["checksum_sha256"] != baseline_checksum:
            baseline_status = "mismatch"
            errors.append("Recorded baseline checksum does not match db/baseline.sql.")
        else:
            baseline_status = "match"

    managed_by_version = {migration.version: migration for migration in managed}
    applied_versions: list[str] = []
    for row in migration_rows:
        version = row["version"]
        migration = managed_by_version.get(version)
        if migration is None:
            errors.append(f"Ledger migration {version} has no corresponding managed file.")
            continue
        if row["checksum_sha256"] != migration.checksum_sha256:
            errors.append(f"Managed migration checksum mismatch for {migration.name}.")
        applied_versions.append(version)
        latest_applied_migration = version

    pending: list[ManagedMigration] = [migration for migration in managed if migration.version not in set(applied_versions)]
    pending_names = [migration.name for migration in pending]

    if applied_versions:
        max_applied = max(applied_versions)
        earlier_pending = [migration.name for migration in pending if migration.version < max_applied]
        if earlier_pending:
            errors.append(
                "Pending managed migrations precede an already-applied later version: "
                + ", ".join(earlier_pending)
            )

    if errors:
        state = "checksum_mismatch" if any("checksum" in error.lower() for error in errors) else "invalid"
    elif pending_names:
        state = "pending"
    else:
        state = "current"

    return {
        "state": state,
        "baseline_version": BASELINE_VERSION,
        "baseline_checksum_status": baseline_status,
        "latest_applied_managed_migration": latest_applied_migration,
        "pending_migrations": pending_names,
        "pending_migration_count": len(pending_names),
        "errors": errors,
    }


def ensure_state_allows_upgrade(state: dict[str, Any]) -> None:
    if state["state"] == "adoption_required":
        raise MigrationError("adoption_required: existing non-empty database must be enrolled with adopt-baseline.")
    if state["state"] in {"invalid", "checksum_mismatch"}:
        raise MigrationError("; ".join(state["errors"]) or f"Database state {state['state']} is not upgradeable.")


def install_baseline(conn: psycopg.Connection[Any], db_dir: Path) -> int:
    baseline_path = db_dir / "baseline.sql"
    baseline_checksum = compute_sha256(baseline_path)
    with conn.transaction():
        create_ledger_table(conn)
        execution_ms = execute_sql_file(conn, baseline_path)
        validation_errors = validate_schema_against_baseline(conn, baseline_path=baseline_path, target_schema="public")
        if validation_errors:
            raise MigrationError("Baseline validation failed after install: " + "; ".join(validation_errors))
        insert_ledger_row(
            conn,
            version=BASELINE_VERSION,
            kind="baseline",
            checksum_sha256=baseline_checksum,
            execution_ms=execution_ms,
        )
    return execution_ms


def adopt_baseline(conn: psycopg.Connection[Any], db_dir: Path) -> int:
    baseline_path = db_dir / "baseline.sql"
    baseline_checksum = compute_sha256(baseline_path)
    started = time.perf_counter()
    with conn.transaction():
        create_ledger_table(conn)
        validation_errors = validate_schema_against_baseline(conn, baseline_path=baseline_path, target_schema="public")
        if validation_errors:
            raise MigrationError("Baseline adoption validation failed: " + "; ".join(validation_errors))
        insert_ledger_row(
            conn,
            version=BASELINE_VERSION,
            kind="baseline",
            checksum_sha256=baseline_checksum,
            execution_ms=int((time.perf_counter() - started) * 1000),
        )
    return int((time.perf_counter() - started) * 1000)


def apply_pending_managed_migrations(conn: psycopg.Connection[Any], db_dir: Path) -> list[str]:
    managed = load_managed_migrations(db_dir)
    state = analyze_state(conn, db_dir)
    ensure_state_allows_upgrade(state)
    applied = set()
    if ledger_exists(conn):
        rows = read_ledger_rows(conn)
        applied = {row["version"] for row in rows if row["kind"] == "migration"}

    applied_now: list[str] = []
    for migration in managed:
        if migration.version in applied:
            continue
        with conn.transaction():
            execution_ms = execute_sql_file(conn, migration.path)
            insert_ledger_row(
                conn,
                version=migration.version,
                kind="migration",
                checksum_sha256=migration.checksum_sha256,
                execution_ms=execution_ms,
            )
        applied_now.append(migration.name)
    return applied_now


def run_status(conn: psycopg.Connection[Any], db_dir: Path) -> dict[str, Any]:
    return analyze_state(conn, db_dir)


def run_check(conn: psycopg.Connection[Any], db_dir: Path) -> dict[str, Any]:
    state = analyze_state(conn, db_dir)
    if state["state"] != "current":
        raise MigrationError("; ".join(state["errors"]) or f"Schema check failed: {state['state']}")
    return state


def run_upgrade(conn: psycopg.Connection[Any], db_dir: Path) -> dict[str, Any]:
    locked = False
    acquire_lock(conn)
    locked = True
    try:
        state = analyze_state(conn, db_dir)
        ensure_state_allows_upgrade(state)
        baseline_installed = False
        if state["state"] == "empty":
            install_baseline(conn, db_dir)
            baseline_installed = True
        applied_now = apply_pending_managed_migrations(conn, db_dir)
        final_state = analyze_state(conn, db_dir)
        if final_state["state"] != "current":
            raise MigrationError("; ".join(final_state["errors"]) or "Schema upgrade did not converge to current.")
        final_state["baseline_installed"] = baseline_installed
        final_state["applied_migrations"] = applied_now
        return final_state
    finally:
        if locked:
            release_lock(conn)


def run_adopt_baseline(conn: psycopg.Connection[Any], db_dir: Path) -> dict[str, Any]:
    locked = False
    acquire_lock(conn)
    locked = True
    try:
        state = analyze_state(conn, db_dir)
        if state["state"] == "empty":
            raise MigrationError("Cannot adopt baseline into an empty database; use upgrade instead.")
        if state["state"] in {"current", "pending", "checksum_mismatch", "invalid"}:
            raise MigrationError("Database already has schema migration ledger state; adopt-baseline is only for untracked databases.")
        adopt_baseline(conn, db_dir)
        final_state = analyze_state(conn, db_dir)
        if final_state["state"] not in {"current", "pending"}:
            raise MigrationError("; ".join(final_state["errors"]) or "Baseline adoption did not produce a valid ledger.")
        final_state["adopted_baseline"] = True
        return final_state
    finally:
        if locked:
            release_lock(conn)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Basic Memory Store schema migration lifecycle runner.")
    parser.add_argument("command", choices=["status", "check", "adopt-baseline", "upgrade"])
    parser.add_argument("--dsn", default=os.environ.get("PG_DSN"), help="PostgreSQL DSN. Defaults to PG_DSN.")
    parser.add_argument("--db-dir", default=None, help="Override the db directory root.")
    return parser


def connect(dsn: str) -> psycopg.Connection[Any]:
    return psycopg.connect(dsn, autocommit=True, row_factory=dict_row)


def main() -> int:
    args = build_parser().parse_args()
    if not args.dsn:
        print_json({"ok": False, "error": "PG_DSN is required."})
        return 2

    dsn = args.dsn
    try:
        db_dir = find_db_dir(args.db_dir)
        with connect(dsn) as conn:
            if args.command == "status":
                payload = run_status(conn, db_dir)
            elif args.command == "check":
                payload = run_check(conn, db_dir)
            elif args.command == "adopt-baseline":
                payload = run_adopt_baseline(conn, db_dir)
            else:
                payload = run_upgrade(conn, db_dir)
            payload["ok"] = True
            print_json(payload)
            return 0
    except Exception as exc:
        message = sanitize_exception(exc, dsn)
        print_json({"ok": False, "error": message})
        return 1


if __name__ == "__main__":
    sys.exit(main())
