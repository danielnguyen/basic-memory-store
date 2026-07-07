#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_BASE = "origin/main"

_CODE_PATTERNS = (
    ("co-id", re.compile(r"\b" + "CO" + r"-[0-9A-Z-]+\b")),
    ("pull-request-id", re.compile(r"\b" + "PR" + r"[0-9]+\b")),
    ("pull-request-marker", re.compile(r"\b" + "PR" + r"\s+#")),
    ("requirement-id", re.compile(r"\b" + "R" + r"[0-9]+\b")),
)

_STATUS_OK = "pa" + "ss"
_STATUS_CONTEXT = re.compile(
    r"(?i)(?:^|[^a-z0-9])"
    + _STATUS_OK
    + r"[_-][a-z0-9]"
    + r"|[a-z0-9][_-]"
    + _STATUS_OK
    + r"(?:$|[^a-z0-9])"
    + r"|\b(?:correction|delivery|implementation|process|review|cleanup)\s+"
    + _STATUS_OK
    + r"\b"
    + r"|\b"
    + _STATUS_OK
    + r"\s+(?:label|name|boundary|artifact)\b"
)

_BASE_TERMS = (
    "sub" + "wa" + "ve",
    "wa" + "ve",
    "pha" + "se",
    "clus" + "ter",
)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int | None
    token: str
    pattern: str
    context: str


def normalize_context(value: str) -> str:
    return " ".join(value.strip().split())


def _term_matches(value: str, term: str) -> bool:
    if value == term:
        return True
    if term == "sub" + "wa" + "ve":
        return False
    if not value.startswith(term):
        return False
    suffix = value[len(term) :]
    return bool(suffix) and suffix[0].isdigit()


def _status_word_matches(text: str, value: str) -> bool:
    if value != _STATUS_OK:
        return False
    if text.strip() == _STATUS_OK:
        return False
    return bool(_STATUS_CONTEXT.search(text))


def detect_in_text(path: str, text: str, line: int | None) -> list[Finding]:
    context = normalize_context(text)
    findings: list[Finding] = []

    for pattern_name, pattern in _CODE_PATTERNS:
        for match in pattern.finditer(text):
            findings.append(
                Finding(
                    path=path,
                    line=line,
                    token=match.group(0),
                    pattern=pattern_name,
                    context=context,
                )
            )

    for segment in re.split(r"[^A-Za-z0-9]+", text):
        if not segment:
            continue
        lowered = segment.lower()
        if _status_word_matches(text, lowered):
            findings.append(
                Finding(
                    path=path,
                    line=line,
                    token=segment,
                    pattern="status-word",
                    context=context,
                )
            )
            continue
        for term in _BASE_TERMS:
            if not _term_matches(lowered, term):
                continue
            findings.append(
                Finding(
                    path=path,
                    line=line,
                    token=segment,
                    pattern="delivery-term",
                    context=context,
                )
            )
            break

    return list(dict.fromkeys(findings))


def _parse_new_line_start(hunk_header: str) -> int | None:
    match = re.search(r"\+(\d+)(?:,\d+)?", hunk_header)
    if match is None:
        return None
    return int(match.group(1))


def scan_diff_text(diff_text: str) -> list[Finding]:
    findings: list[Finding] = []
    current_path: str | None = None
    new_line: int | None = None
    next_path_is_new = False
    current_path_is_new = False

    for raw_line in diff_text.splitlines():
        if raw_line.startswith("diff --git "):
            current_path = None
            new_line = None
            next_path_is_new = False
            current_path_is_new = False
            continue

        if raw_line.startswith("new file mode "):
            next_path_is_new = True
            continue

        if raw_line.startswith("+++ "):
            marker_path = raw_line[4:]
            if marker_path == "/dev/null":
                current_path = None
                current_path_is_new = False
                continue
            current_path = marker_path[2:] if marker_path.startswith("b/") else marker_path
            current_path_is_new = next_path_is_new
            if current_path_is_new:
                findings.extend(detect_in_text(current_path, current_path, None))
            continue

        if raw_line.startswith("@@ "):
            new_line = _parse_new_line_start(raw_line)
            continue

        if current_path is None:
            continue

        if raw_line.startswith("+"):
            if raw_line.startswith("+++ "):
                continue
            text = raw_line[1:]
            findings.extend(detect_in_text(current_path, text, new_line))
            if new_line is not None:
                new_line += 1
            continue

        if raw_line.startswith("-"):
            continue

        if new_line is not None:
            new_line += 1

    return findings


def run_git_diff(base: str, root: Path) -> str:
    command = [
        "git",
        "-C",
        str(root),
        "diff",
        "--unified=0",
        "--no-color",
        "--no-ext-diff",
        f"{base}...HEAD",
    ]
    result = subprocess.run(command, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0:
        print(result.stderr.strip() or f"git diff failed for base {base}", file=sys.stderr)
        raise SystemExit(result.returncode)
    return result.stdout


def print_report(findings: list[Finding], base: str) -> None:
    print("Process naming diff guardrail")
    print(f"Base: {base}")
    print(f"Added-line findings: {len(findings)}")
    if not findings:
        return

    print("New public-repo wording findings:")
    for finding in findings:
        location = finding.path
        if finding.line is not None:
            location = f"{location}:{finding.line}"
        print(f"  - {location}: token={finding.token!r} pattern={finding.pattern} context={finding.context!r}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--base",
        default=DEFAULT_BASE,
        help="git ref used as the merge-base comparison point",
    )
    parser.add_argument(
        "--root",
        default=".",
        help="repository root",
    )
    args = parser.parse_args()

    root = Path(args.root).resolve()
    diff_text = run_git_diff(args.base, root)
    findings = scan_diff_text(diff_text)
    print_report(findings, args.base)
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
