"""
Static audit: catch MySQL-style boolean SQL (column = 0/1) that breaks PostgreSQL BOOLEAN columns.

Run: python v2_api/pg_sql_audit.py
Exit 0 = clean, 2 = findings (use in preflight / CI).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import List, Tuple

# Repo root (parent of v2_api/)
ROOT = Path(__file__).resolve().parent.parent

SCAN_DIRS = ("v2_api", "scripts", "scanner_performance.py", "bigid_auth.py", "benchmark_scenario_normalizer.py")

# Patterns that are unsafe on PostgreSQL BOOLEAN columns in raw SQL strings.
UNSAFE_PATTERNS: List[Tuple[str, str]] = [
    (r"\bis_active\s*=\s*1\b", "is_active = 1"),
    (r"\bis_active\s*=\s*0\b", "is_active = 0"),
    (r"\bis_busy\s*=\s*1\b", "is_busy = 1"),
    (r"\bis_busy\s*=\s*0\b", "is_busy = 0"),
    (r"\bte\.is_active\s*=\s*1\b", "te.is_active = 1"),
    (r"\bCOALESCE\s*\(\s*is_busy\s*,\s*0\s*\)\s*=\s*1", "COALESCE(is_busy, 0) = 1"),
    (r"\bCOALESCE\s*\(\s*is_active\s*,\s*0\s*\)\s*=\s*1", "COALESCE(is_active, 0) = 1"),
]

# Lines that intentionally mention legacy SQL (audit probes, comments about the bug).
ALLOW_SUBSTRINGS = (
    "legacy is_active",
    "PostgreSQL uses BOOLEAN",
    "pg_sql_audit",
    "UNSAFE_PATTERNS",
    "audit probe",
)


def _should_scan(path: Path) -> bool:
    if path.name == "pg_sql_audit.py":
        return False
    if path.suffix != ".py":
        return False
    try:
        rel = path.relative_to(ROOT)
    except ValueError:
        return False
    parts = rel.parts
    if parts[0] == "v2_api":
        return True
    if parts[0] == "scripts":
        return True
    if len(parts) == 1 and parts[0] in ("scanner_performance.py", "bigid_auth.py", "benchmark_scenario_normalizer.py"):
        return True
    return False


def audit_repo(root: Path = ROOT) -> dict:
    findings: List[dict] = []
    for path in sorted(root.rglob("*.py")):
        if not _should_scan(path):
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, start=1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("'''"):
                continue
            # Docstring / comment lines inside functions (no executable SQL)
            if '"""' in line or "'''" in line:
                if not re.search(r'\b(text|execute|SELECT|UPDATE|INSERT|DELETE|FROM|WHERE)\b', line, re.I):
                    continue
            if any(a in line for a in ALLOW_SUBSTRINGS):
                continue
            for pattern, label in UNSAFE_PATTERNS:
                if not re.search(
                    r"\b(text|execute|SELECT|UPDATE|INSERT|DELETE|FROM|WHERE|JOIN)\b",
                    line,
                    re.IGNORECASE,
                ):
                    continue
                if re.search(pattern, line, re.IGNORECASE):
                    findings.append(
                        {
                            "file": str(path.relative_to(root)),
                            "line": i,
                            "issue": label,
                            "snippet": stripped[:200],
                        }
                    )
                    break
    return {"ok": len(findings) == 0, "findings_count": len(findings), "findings": findings}


def main() -> int:
    result = audit_repo()
    if result["ok"]:
        print("pg_sql_audit: OK — no MySQL-style boolean SQL in scanned Python files.")
        return 0
    print(f"pg_sql_audit: FAIL — {result['findings_count']} issue(s):\n")
    for f in result["findings"]:
        print(f"  {f['file']}:{f['line']}  {f['issue']}")
        print(f"    {f['snippet']}\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
