#!/usr/bin/env python3
"""Exercise the disposable maintainer archive/recovery workflow."""
from __future__ import annotations

import json

from worktree_conservator.demo import run_demo


def main() -> None:
    result = run_demo()
    checks = {
        "scan": result.get("planned", 0) == 1,
        "plan": result.get("planned", 0) == 1,
        "apply": result.get("applied", 0) == 1,
        "verify": result.get("verified") is True,
        "audit": result.get("audited") is True,
        "restore": result.get("restored") is True,
    }
    output = {
        "schema": "worktree-conservator/integration-example/v1",
        "workflow": "disposable-maintainer-recovery",
        "steps": checks,
        "archive_verified": checks["verify"] and checks["audit"],
        "restored": checks["restore"],
    }
    print(json.dumps(output, indent=2, sort_keys=True))
    if not all(checks.values()) or not output["archive_verified"] or not output["restored"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
