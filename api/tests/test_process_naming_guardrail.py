from __future__ import annotations

import importlib.util
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


def diff_for(path: str, *lines: str, new_file: bool = False) -> str:
    header = [f"diff --git a/{path} b/{path}"]
    if new_file:
        header.append("new file mode 100644")
        header.append("--- /dev/null")
    else:
        header.append(f"--- a/{path}")
    header.append(f"+++ b/{path}")
    header.append(f"@@ -0,0 +1,{len(lines)} @@")
    return "\n".join([*header, *lines, ""])


def test_added_forbidden_term_fails_anywhere() -> None:
    scanner = load_scanner()
    bad = "wa" + "ve3b"
    findings = scanner.scan_diff_text(diff_for("README.md", f"+{bad} result boundary"))
    assert [(finding.path, finding.token, finding.line) for finding in findings] == [
        ("README.md", bad, 1)
    ]


def test_added_new_file_path_fails() -> None:
    scanner = load_scanner()
    bad = "clus" + "ter20"
    path = f"docs/{bad}.md"
    findings = scanner.scan_diff_text(diff_for(path, "+clean text", new_file=True))
    assert [(finding.path, finding.token, finding.line) for finding in findings] == [
        (path, bad, None)
    ]


def test_existing_dirty_file_path_is_not_reflagged_for_clean_edit() -> None:
    scanner = load_scanner()
    bad = "wa" + "ve3b"
    path = f"scripts/{bad}_smoke.sh"
    findings = scanner.scan_diff_text(diff_for(path, "+echo clean"))
    assert findings == []


def test_deleted_terms_are_ignored() -> None:
    scanner = load_scanner()
    bad = "pha" + "se4"
    findings = scanner.scan_diff_text(diff_for("README.md", f"-{bad} old wording"))
    assert findings == []
