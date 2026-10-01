"""Disposable end-to-end demonstration; creates only a temporary Git repository."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from .core import apply_plan, make_plan, restore, scan, verify_archive


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=Demo", "-c", "user.email=demo@example.invalid",
                    "-c", "core.fsmonitor=false", "-C", str(repo), *args],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def run_demo() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="worktree-conservator-demo-") as raw:
        root = Path(raw).resolve()
        repo = root / "repo"
        worktrees = root / "worktrees"
        worktrees.mkdir()
        archives = root / "archives"
        recovered = worktrees / "recovered"
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "main")
        (repo / "README.txt").write_text("demo base\n", encoding="utf-8")
        _git(repo, "add", "README.txt")
        _git(repo, "commit", "-qm", "base")
        head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
        _git(repo, "update-ref", "refs/remotes/origin/main", head)
        candidate = worktrees / "old-clean"
        _git(repo, "worktree", "add", "-q", "--detach", str(candidate), head)
        old = candidate.stat().st_mtime - 48 * 3600
        os.utime(candidate, (old, old))
        observed = scan(repo, root_arg=worktrees, base_ref="origin/main", min_age_hours=0)
        plan = make_plan(observed, archives)
        plan_path = root / "plan.json"
        plan_path.write_text(json.dumps(plan, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        applied = apply_plan(plan_path, plan["plan_sha256"], repo, archives, worktrees)
        archived = applied["applied"][0]
        verified = verify_archive(archived["archive"], archived["archive_sha256"], repo, archived["receipt"])
        restored = restore(archived["archive"], archived["archive_sha256"], repo, recovered)
        return {"demo": "worktree-conservator/v1", "planned": len(plan["payload"]["candidates"]),
                "applied": len(applied["applied"]), "verified": verified["git_content_verified"],
                "restored": restored["clean"], "head": restored["head"]}


def main() -> int:
    print(json.dumps(run_demo(), sort_keys=True, separators=(",", ":")))
    return 0
