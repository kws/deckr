"""Enforce the authority lifecycle branch-coverage acceptance gate."""

from __future__ import annotations

import json
import sys
from pathlib import Path

MINIMUM_BRANCH_COVERAGE = 95.0
LIFECYCLE_MODULES = (
    "src/deckr/_concord/_agreement.py",
    "src/deckr/_concord/_lease.py",
    "src/deckr/_concord/_owner.py",
    "src/deckr/_concord/_participant.py",
    "src/deckr/beacon.py",
)


def main() -> int:
    report_path = Path(sys.argv[1] if len(sys.argv) > 1 else "coverage.json")
    report = json.loads(report_path.read_text())
    failures: list[str] = []
    for module in LIFECYCLE_MODULES:
        summary = report["files"][module]["summary"]
        covered = summary["covered_branches"]
        total = summary["num_branches"]
        percentage = summary["percent_branches_covered"]
        print(f"{module}: {percentage:.2f}% ({covered}/{total} branches)")
        if percentage < MINIMUM_BRANCH_COVERAGE:
            failures.append(f"{module}: {percentage:.2f}%")
    if failures:
        print(
            f"Lifecycle branch coverage must be at least "
            f"{MINIMUM_BRANCH_COVERAGE:.0f}%: {', '.join(failures)}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
