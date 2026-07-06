from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "check_process_naming.py"


def load_scanner():
    spec = importlib.util.spec_from_file_location("check_process_naming", SCRIPT_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_allowlist(root: Path, entries: list[dict[str, object]]) -> Path:
    allowlist = root / "allowlist.json"
    allowlist.write_text(json.dumps({"schema_version": 1, "entries": entries}), encoding="utf-8")
    return allowlist


def write_file(root: Path, relative_path: str, content: str) -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def valid_allowlist_entry(**overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "path": "api/services/episode_intelligence.py",
        "token": "wave",
        "pattern": "wave",
        "bucket": "runtime evidence taxonomy compatibility debt",
        "reason": "Existing evidence extraction term.",
        "context_contains": "evidence_terms = ('wave',)",
        "reviewed_at": "2026-07-06",
        "removal_required_later": True,
    }
    entry.update(overrides)
    return entry


def test_new_production_process_symbol_fails(tmp_path: Path) -> None:
    scanner = load_scanner()
    write_file(tmp_path, "api/services/example.py", "wave3b_result_boundary = True\n")
    report = scanner.scan_repository(tmp_path, write_allowlist(tmp_path, []))
    assert [finding.token for finding in report.violations] == ["wave3b"]


def test_allowlisted_existing_finding_passes(tmp_path: Path) -> None:
    scanner = load_scanner()
    write_file(tmp_path, "api/services/episode_intelligence.py", "evidence_terms = ('wave',)\n")
    report = scanner.scan_repository(tmp_path, write_allowlist(tmp_path, [valid_allowlist_entry()]))
    assert report.violations == []
    assert len(report.allowlisted_findings) == 1


def test_path_only_allowlist_entry_fails_schema_validation(tmp_path: Path) -> None:
    scanner = load_scanner()
    allowlist = write_allowlist(tmp_path, [{"path": "api/services/episode_intelligence.py"}])
    try:
        scanner.load_allowlist(allowlist)
    except ValueError as exc:
        assert "token or pattern" in str(exc)
    else:
        raise AssertionError("path-only allowlist entry should fail validation")


def test_allowlist_entry_missing_token_and_pattern_fails(tmp_path: Path) -> None:
    scanner = load_scanner()
    entry = valid_allowlist_entry()
    del entry["token"]
    del entry["pattern"]
    allowlist = write_allowlist(tmp_path, [entry])
    try:
        scanner.load_allowlist(allowlist)
    except ValueError as exc:
        assert "token or pattern" in str(exc)
    else:
        raise AssertionError("allowlist entry without token or pattern should fail")


def test_allowlist_entry_missing_required_metadata_fails(tmp_path: Path) -> None:
    scanner = load_scanner()
    for missing_field in (
        "reason",
        "bucket",
        "context_contains",
        "removal_required_later",
        "reviewed_at",
    ):
        entry = valid_allowlist_entry()
        del entry[missing_field]
        allowlist = write_allowlist(tmp_path, [entry])
        try:
            scanner.load_allowlist(allowlist)
        except ValueError as exc:
            expected = "reviewed_at or reviewed_by" if missing_field == "reviewed_at" else missing_field
            assert expected in str(exc)
        else:
            raise AssertionError(f"allowlist entry without {missing_field} should fail")


def test_excluded_paths_are_ignored(tmp_path: Path) -> None:
    scanner = load_scanner()
    write_file(tmp_path, "api/tests/test_wave3b.py", "wave3b_result_boundary = True\n")
    write_file(tmp_path, "scripts/wave3b_smoke.sh", "echo wave3b\n")
    write_file(tmp_path, "db/migrations/001_wave3b.sql", "select 'wave3b';\n")
    write_file(tmp_path, "db/base_baseline.sql", "select 'wave3b';\n")
    report = scanner.scan_repository(tmp_path, write_allowlist(tmp_path, []))
    assert report.scanned_files == 0
    assert report.violations == []


def test_pass_false_positives_are_not_flagged(tmp_path: Path) -> None:
    scanner = load_scanner()
    write_file(
        tmp_path,
        "api/services/auth.py",
        "\n".join(
            [
                "password = 'redacted'",
                "passkey = 'redacted'",
                "passed = True",
                "passing = True",
                "bypass = False",
                "passage = 'text'",
                "cursor_description = 'pass back as cursor'",
                "",
            ]
        ),
    )
    report = scanner.scan_repository(tmp_path, write_allowlist(tmp_path, []))
    assert report.violations == []
