"""Verify that security and CI release gates are tracked."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_TRACKED = frozenset(
    {
        ".github/workflows/ci.yml",
        "scripts/check_release_state.py",
        "scripts/check_secrets.py",
    }
)
FORBIDDEN_TRACKED = frozenset({"api.txt", "config/.env"})


def release_violations(tracked: set[str]) -> list[str]:
    normalized = {item.replace("\\", "/") for item in tracked}
    findings = [
        f"{path}: forbidden credential file is tracked"
        for path in sorted(FORBIDDEN_TRACKED & normalized)
    ]
    findings.extend(
        f"{path}: required release file is not tracked"
        for path in sorted(REQUIRED_TRACKED - normalized)
    )
    return findings


def tracked_files() -> set[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    )
    return {
        item.decode("utf-8", errors="surrogateescape")
        for item in result.stdout.split(b"\0")
        if item
    }


def main() -> int:
    findings = release_violations(tracked_files())
    if findings:
        print("Release policy violations:", file=sys.stderr)
        for finding in findings:
            print(f"- {finding}", file=sys.stderr)
        return 1
    print("Release policy check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
