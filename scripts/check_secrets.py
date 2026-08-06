"""Fail CI when tracked files contain obvious credentials or secret file names.

This is intentionally deterministic and dependency-free.  It reports only the
file and line number, never the matched value.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_PATHS = {
    "api.txt",
    "config/.env",
}
SECRET_ASSIGNMENT = re.compile(
    rb"(?m)^[ \t]*(?:ALPACA_KEY|ALPACA_SECRET|FEISHU_APP_SECRET|FEISHU_WEBHOOK)"
    rb"[ \t]*[:=][ \t]*([^\r\n]*)$"
)
PLACEHOLDER_VALUES = {"example", "sample", "placeholder", "changeme"}


def tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    )
    return [
        item.decode("utf-8", errors="surrogateescape")
        for item in result.stdout.split(b"\0")
        if item
    ]


def main() -> int:
    findings: list[str] = []
    for relative in tracked_files():
        normalized = relative.replace("\\", "/")
        if normalized.lower() in FORBIDDEN_PATHS:
            findings.append(f"{normalized}: forbidden credential file is tracked")
            continue
        path = REPO_ROOT / relative
        try:
            content = path.read_bytes()
        except (OSError, IsADirectoryError):
            continue
        for match in SECRET_ASSIGNMENT.finditer(content):
            value = match.group(1).strip().strip(b"'\"")
            if not value or value.startswith(b"#"):
                continue
            display_value = value.decode("utf-8", errors="ignore").lower()
            if (
                display_value.startswith(("你的", "<", "${"))
                or display_value in PLACEHOLDER_VALUES
            ):
                continue
            line = content.count(b"\n", 0, match.start()) + 1
            findings.append(f"{normalized}:{line}: non-empty credential assignment")

    if findings:
        print("Secret policy violations:", file=sys.stderr)
        for finding in findings:
            print(f"- {finding}", file=sys.stderr)
        return 1
    print("Secret policy check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
