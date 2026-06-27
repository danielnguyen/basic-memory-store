from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
LEGACY_VALUES = ("r20-" + "mvp-v1", "r21-" + "m0-v1")
TEXT_SUFFIXES = {".py", ".sql", ".md", ".txt", ".yml", ".yaml", ".toml"}
ALLOWED_PREFIXES = (
    Path("db/migrations/legacy"),
)
ALLOWED_FILES = {
    Path("api/tests/test_schema_migrations_integration.py"),
    Path("db/migrations/managed/20260627120000_derivation_version_cleanup.sql"),
}


def _is_allowed(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    return rel in ALLOWED_FILES or any(rel == prefix or prefix in rel.parents for prefix in ALLOWED_PREFIXES)


def scan() -> list[str]:
    violations: list[str] = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT)
        if ".git" in rel.parts or "__pycache__" in rel.parts or ".pytest_cache" in rel.parts:
            continue
        if path.name == "derivation_version_scan.py":
            continue
        if path.suffix not in TEXT_SUFFIXES and path.name != "Makefile":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for value in LEGACY_VALUES:
            if value in text and not _is_allowed(path):
                violations.append(f"{rel}: contains legacy derivation version {value}")
    return violations


def main() -> int:
    violations = scan()
    if violations:
        print("Legacy planning-linked derivation versions remain in production locations:")
        for violation in violations:
            print(f"- {violation}")
        return 1
    print("derivation-version-test: no legacy derivation versions in production locations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
