from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .core import ConservatorError, _atomic_json, apply_plan, audit_archive_dir, make_plan, restore, scan, verify_archive
from .demo import run_demo

EXIT_SUCCESS = 0
EXIT_USAGE = 2
EXIT_UNSAFE = 3
EXIT_ARCHIVE = 4
EXIT_GIT_IO = 5


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="worktree-conservator",
        description="Preserve-first, side-effect-free-by-default Git worktree conservator.",
        epilog="stdout is one stable JSON result object; diagnostics are sent to stderr. "
               "apply requires the exact plan file and --plan-sha256 emitted by plan.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(command: argparse.ArgumentParser) -> None:
        command.add_argument("--repo", required=True, help="explicit Git repository/worktree root")
        command.add_argument("--root", help="only consider registered worktrees below this existing directory")
        command.add_argument("--base-ref", default="origin/main", help="commit/ref that must contain each candidate")
        command.add_argument("--min-age-hours", type=float, default=24.0)
        command.add_argument("--protected", action="append", default=[], metavar="PATH",
                             help="additional protected path; may be repeated")
        command.add_argument("--json", action="store_true", help="accepted for stable JSON output (always enabled)")

    scan_p = sub.add_parser("scan", help="read-only classify registered worktrees")
    common(scan_p)

    plan_p = sub.add_parser("plan", help="read-only write an exact immutable apply plan")
    common(plan_p)
    plan_p.add_argument("--archive-dir", required=True, help="new or existing directory outside repo for archives/receipts")
    plan_p.add_argument("--output", required=True, help="new plan JSON path")

    apply_p = sub.add_parser("apply", help="apply an exact reviewed plan; never regenerates it")
    apply_p.add_argument("--repo", required=True)
    apply_p.add_argument("--root")
    apply_p.add_argument("--plan", required=True, help="plan JSON produced by plan")
    apply_p.add_argument("--plan-sha256", required=True)
    apply_p.add_argument("--archive-dir", required=True)
    apply_p.add_argument("--json", action="store_true")

    restore_p = sub.add_parser("restore", help="restore a verified archive into a new absent Git worktree")
    restore_p.add_argument("--repo", required=True)
    restore_p.add_argument("--archive", required=True)
    restore_p.add_argument("--archive-sha256", required=True)
    restore_p.add_argument("--target", required=True)
    restore_p.add_argument("--json", action="store_true")

    verify_p = sub.add_parser("verify", help="read-only verify a retained archive against Git and an optional apply receipt")
    verify_p.add_argument("--repo", required=True)
    verify_p.add_argument("--archive", required=True)
    verify_p.add_argument("--archive-sha256", required=True)
    verify_p.add_argument("--receipt", help="optional apply receipt to bind to the archive")
    verify_p.add_argument("--json", action="store_true")

    audit_p = sub.add_parser("audit", help="read-only reconcile archives, receipts, and the recovery journal")
    audit_p.add_argument("--repo", required=True, help="explicit Git repository/worktree root")
    audit_p.add_argument("--archive-dir", required=True, help="existing archive directory produced by apply")
    audit_p.add_argument("--plan", help="optional exact plan JSON to bind every operation")
    audit_p.add_argument("--plan-sha256", help="trusted SHA-256 for --plan; both plan options are required together")
    audit_p.add_argument("--json", action="store_true")

    demo_p = sub.add_parser("demo", help="run an end-to-end disposable temporary-repository demo")
    demo_p.add_argument("--json", action="store_true")
    return parser


def _result(command: str, *, ok: bool, data: Any = None, errors: list[dict[str, Any]] | None = None,
            warnings: list[str] | None = None) -> dict[str, Any]:
    return {"schema": "worktree-conservator.result/v1", "command": command, "ok": ok,
            "data": data if data is not None else {}, "warnings": warnings or [], "errors": errors or []}


def _write_json(path: str, value: dict[str, Any]) -> None:
    target = Path(path).expanduser()
    if target.exists() or target.is_symlink():
        raise ConservatorError("output_exists", "refusing to overwrite an existing plan path", EXIT_UNSAFE)
    _atomic_json(target, value, replace=False)


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
        if args.command == "demo":
            output = _result("demo", ok=True, data=run_demo())
            print(json.dumps(output, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            return EXIT_SUCCESS
        if args.command == "scan":
            data = scan(args.repo, root_arg=args.root, base_ref=args.base_ref, min_age_hours=args.min_age_hours,
                        protected_args=args.protected)
            output = _result("scan", ok=True, data=data)
        elif args.command == "plan":
            data = scan(args.repo, root_arg=args.root, base_ref=args.base_ref, min_age_hours=args.min_age_hours,
                        protected_args=args.protected)
            plan = make_plan(data, args.archive_dir)
            _write_json(args.output, plan)
            output = _result("plan", ok=True, data={"plan": plan, "plan_path": str(Path(args.output).expanduser().resolve())})
        elif args.command == "apply":
            data = apply_plan(args.plan, args.plan_sha256.lower(), args.repo, args.archive_dir, args.root)
            output = _result("apply", ok=True, data=data)
        elif args.command == "restore":
            data = restore(args.archive, args.archive_sha256.lower(), args.repo, args.target)
            output = _result("restore", ok=True, data=data)
        elif args.command == "verify":
            data = verify_archive(args.archive, args.archive_sha256.lower(), args.repo, args.receipt)
            output = _result("verify", ok=True, data=data)
        elif args.command == "audit":
            data = audit_archive_dir(args.archive_dir, args.repo, args.plan,
                                     args.plan_sha256.lower() if args.plan_sha256 else None)
            output = _result("audit", ok=True, data=data)
        else:
            data = run_demo()
            output = _result("demo", ok=True, data=data)
        print(json.dumps(output, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return EXIT_SUCCESS
    except ConservatorError as exc:
        error = {"code": exc.code, "message": exc.message, **exc.details}
        output = _result(getattr(locals().get("args", None), "command", "unknown"), ok=False, errors=[error])
        print(json.dumps(output, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return exc.exit_code
    except (OSError, ValueError, TypeError) as exc:
        output = _result(getattr(locals().get("args", None), "command", "unknown"), ok=False,
                         errors=[{"code": "unexpected_error", "message": str(exc)}])
        print(json.dumps(output, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return EXIT_GIT_IO


if __name__ == "__main__":
    raise SystemExit(main())
