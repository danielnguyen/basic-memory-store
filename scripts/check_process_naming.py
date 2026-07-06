#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_SCAN_PATHS = (
    "api/services",
    "api/clients",
    "api/models.py",
    "api/storage",
    "api/main.py",
)

EXCLUDED_CATEGORIES = {
    "tests": ("api/tests", "tests"),
    "smoke scripts": ("scripts/*smoke*", "api/*smoke*.py"),
    "Makefile smoke target names": ("Makefile",),
    "docs": ("README.md", "docs"),
    "docker compose files": ("docker-compose*.yml",),
    "migrations": ("db/migrations", "api/migrations"),
    "baseline SQL": ("db/*baseline*.sql",),
    "fixtures": ("fixtures", "api/fixtures"),
    "generated/cache files": ("__pycache__", ".pytest_cache", ".ruff_cache"),
    "legacy validation tools/harnesses": ("api/tools", "api/replay"),
}

CODE_PATTERNS = (
    ("CO-[0-9A-Z-]+", re.compile(r"\bCO-[0-9A-Z-]+\b")),
    ("PR[0-9]+", re.compile(r"\bPR[0-9]+\b")),
    ("PR #", re.compile(r"\bPR\s+#")),
    ("R[0-9]+", re.compile(r"\bR[0-9]+\b")),
)

WORD_PATTERNS = (
    ("subwave", re.compile(r"^subwave$")),
    ("wave[0-9]", re.compile(r"^wave[0-9][a-z0-9]*$")),
    ("wave", re.compile(r"^wave$")),
    ("phase[0-9]", re.compile(r"^phase[0-9][a-z0-9]*$")),
    ("phase", re.compile(r"^phase$")),
    ("cluster[0-9]", re.compile(r"^cluster[0-9][a-z0-9]*$")),
    ("cluster", re.compile(r"^cluster$")),
)

PROCESS_PASS_CONTEXT = re.compile(
    r"(?i)(?:^|[^a-z0-9])pass[_-][a-z0-9]"
    r"|[a-z0-9][_-]pass(?:$|[^a-z0-9])"
    r"|\b(?:correction|delivery|implementation|process|review|cleanup)\s+pass\b"
    r"|\bpass\s+(?:label|name|boundary|artifact)\b"
)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int | None
    token: str
    pattern: str
    bucket: str
    context: str


@dataclass(frozen=True)
class Report:
    scanned_files: int
    allowlisted_findings: list[Finding]
    violations: list[Finding]
    excluded_categories: dict[str, tuple[str, ...]]


def normalize_context(value: str) -> str:
    return " ".join(value.strip().split())


def iter_scan_files(root: Path, configured_paths: tuple[str, ...]) -> list[Path]:
    files: list[Path] = []
    for configured_path in configured_paths:
        target = root / configured_path
        if not target.exists():
            continue
        if target.is_file():
            candidates = [target]
        else:
            candidates = [path for path in target.rglob("*.py") if path.is_file()]
        for path in candidates:
            rel = path.relative_to(root)
            if any(part in {"__pycache__", ".pytest_cache", ".ruff_cache"} for part in rel.parts):
                continue
            files.append(path)
    return sorted(set(files))


def _pass_is_process_word(text: str, segment: str) -> bool:
    if segment.lower() != "pass":
        return False
    if text.strip() == "pass":
        return False
    return bool(PROCESS_PASS_CONTEXT.search(text))


def detect_in_text(path: str, text: str, line: int | None) -> list[Finding]:
    context = normalize_context(text)
    findings: list[Finding] = []
    for pattern_name, pattern in CODE_PATTERNS:
        for match in pattern.finditer(text):
            findings.append(
                Finding(
                    path=path,
                    line=line,
                    token=match.group(0),
                    pattern=pattern_name,
                    bucket="process-code",
                    context=context,
                )
            )

    for segment in re.split(r"[^A-Za-z0-9]+", text):
        if not segment:
            continue
        lowered = segment.lower()
        if _pass_is_process_word(text, segment):
            findings.append(
                Finding(
                    path=path,
                    line=line,
                    token=segment,
                    pattern="pass",
                    bucket="process-word",
                    context=context,
                )
            )
            continue
        for pattern_name, pattern in WORD_PATTERNS:
            if not pattern.fullmatch(lowered):
                continue
            findings.append(
                Finding(
                    path=path,
                    line=line,
                    token=segment,
                    pattern=pattern_name,
                    bucket="process-word",
                    context=context,
                )
            )
            break
    return findings


def find_process_names(root: Path, configured_paths: tuple[str, ...]) -> tuple[int, list[Finding]]:
    findings: list[Finding] = []
    scan_files = iter_scan_files(root, configured_paths)
    for path in scan_files:
        rel = path.relative_to(root).as_posix()
        findings.extend(detect_in_text(rel, path.name, None))
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            content = path.read_text(encoding="utf-8", errors="replace")
        for line_number, line in enumerate(content.splitlines(), start=1):
            findings.extend(detect_in_text(rel, line, line_number))
    return len(scan_files), findings


def load_allowlist(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise ValueError("allowlist must contain an entries list")
    validate_allowlist_entries(entries)
    return entries


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_allowlist_entries(entries: list[Any]) -> None:
    for index, entry in enumerate(entries):
        label = f"allowlist entry {index}"
        if not isinstance(entry, dict):
            raise ValueError(f"{label} must be an object")
        if not _non_empty_string(entry.get("path")):
            raise ValueError(f"{label} must include a non-empty path")
        if not (_non_empty_string(entry.get("token")) or _non_empty_string(entry.get("pattern"))):
            raise ValueError(f"{label} must include token or pattern")
        for field in ("bucket", "reason"):
            if not _non_empty_string(entry.get(field)):
                raise ValueError(f"{label} must include a non-empty {field}")
        if not _non_empty_string(entry.get("context_contains")):
            raise ValueError(f"{label} must include non-empty context_contains")
        if "removal_required_later" not in entry:
            raise ValueError(f"{label} must include removal_required_later")
        if not isinstance(entry["removal_required_later"], bool):
            raise ValueError(f"{label} removal_required_later must be a boolean")
        if not (
            _non_empty_string(entry.get("reviewed_at"))
            or _non_empty_string(entry.get("reviewed_by"))
        ):
            raise ValueError(f"{label} must include reviewed_at or reviewed_by")


def allowlist_matches(finding: Finding, entry: dict[str, Any]) -> bool:
    if entry.get("path") != finding.path:
        return False
    token = entry.get("token")
    pattern = entry.get("pattern")
    if token is not None and str(token).lower() != finding.token.lower():
        return False
    if pattern is not None and pattern != finding.pattern:
        return False
    context_contains = entry.get("context_contains")
    if context_contains is not None and str(context_contains) not in finding.context:
        return False
    return True


def split_findings(
    findings: list[Finding],
    allowlist_entries: list[dict[str, Any]],
) -> tuple[list[Finding], list[Finding]]:
    allowlisted: list[Finding] = []
    violations: list[Finding] = []
    for finding in findings:
        if any(allowlist_matches(finding, entry) for entry in allowlist_entries):
            allowlisted.append(finding)
        else:
            violations.append(finding)
    return allowlisted, violations


def scan_repository(
    root: Path,
    allowlist_path: Path,
    configured_paths: tuple[str, ...] = DEFAULT_SCAN_PATHS,
) -> Report:
    scanned_files, findings = find_process_names(root, configured_paths)
    allowlist_entries = load_allowlist(allowlist_path)
    allowlisted, violations = split_findings(findings, allowlist_entries)
    return Report(
        scanned_files=scanned_files,
        allowlisted_findings=allowlisted,
        violations=violations,
        excluded_categories=EXCLUDED_CATEGORIES,
    )


def print_report(report: Report) -> None:
    print("Process naming guardrail")
    print(f"Scanned files: {report.scanned_files}")
    print(f"Allowlisted existing debt findings: {len(report.allowlisted_findings)}")
    print(f"Net-new unallowlisted findings: {len(report.violations)}")
    print("Excluded path categories:")
    for category, patterns in report.excluded_categories.items():
        print(f"  - {category}: {', '.join(patterns)}")
    print(
        "Reminder: allowlisted entries are debt unless explicitly marked as "
        "domain-approved."
    )
    if report.violations:
        print("New unallowlisted findings:")
        for finding in report.violations:
            location = finding.path
            if finding.line is not None:
                location = f"{location}:{finding.line}"
            print(
                f"  - {location}: token={finding.token!r} pattern={finding.pattern!r} "
                f"bucket={finding.bucket} context={finding.context!r}"
            )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check production code for process names.")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument(
        "--allowlist",
        type=Path,
        default=Path(__file__).with_name("process_naming_allowlist.json"),
    )
    parser.add_argument("--scan-path", action="append", dest="scan_paths")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    configured_paths = tuple(args.scan_paths) if args.scan_paths else DEFAULT_SCAN_PATHS
    report = scan_repository(args.root.resolve(), args.allowlist.resolve(), configured_paths)
    print_report(report)
    return 1 if report.violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
