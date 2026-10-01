from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
from typing import Any, Iterable

VERSION = "0.3.0"
PLAN_SCHEMA = "worktree-conservator.plan/v1"
RESULT_SCHEMA = "worktree-conservator.result/v1"
MANIFEST_SCHEMA = "worktree-conservator.archive-manifest/v1"
RECEIPT_SCHEMA = "worktree-conservator.receipt/v1"
JOURNAL_SCHEMA = "worktree-conservator.journal/v1"
AUDIT_SCHEMA = "worktree-conservator.audit/v1"
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
MAX_MEMBERS = 200_000
MAX_MEMBER_BYTES = 512 * 1024 * 1024
MAX_MANIFEST_BYTES = 32 * 1024 * 1024


class ConservatorError(Exception):
    def __init__(self, code: str, message: str, exit_code: int = 3, **details: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.details = details


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def plan_digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(b"worktree-conservator-plan/v1\0" + canonical_json(payload)).hexdigest()


def _pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate JSON key")
        out[key] = value
    return out


def strict_json(data: bytes) -> Any:
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_pairs_no_duplicates,
                          parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("non-finite JSON number")))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ConservatorError("invalid_json", "JSON input is malformed or ambiguous", 2) from exc


def _clean_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
                "LC_ALL": "C", "LANG": "C", "HOME": os.environ.get("HOME", "/")})
    return env


def git(repo: Path, *args: str, timeout: float = 45, input_data: bytes | None = None,
        stdout_file: int | None = None) -> bytes:
    argv = ["git", "-c", "core.fsmonitor=false",
            "-c", "maintenance.auto=false", "-c", "gc.auto=0", "-c", "core.attributesFile=" + os.devnull,
            "-C", str(repo), *args]
    try:
        proc = subprocess.run(argv, input=input_data, stdout=stdout_file if stdout_file is not None else subprocess.PIPE,
                              stderr=subprocess.PIPE, env=_clean_env(), timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConservatorError("git_unavailable", "required Git evidence is unavailable", 5,
                               operation=args[0] if args else "git") from exc
    if proc.returncode:
        raise ConservatorError("git_evidence_failed", "Git could not prove the requested state", 5,
                               operation=args[0] if args else "git", returncode=proc.returncode)
    return proc.stdout or b""


def git_status(repo: Path, *args: str, allow_failure: bool = False) -> tuple[int, bytes]:
    argv = ["git", "-c", "core.fsmonitor=false",
            "-c", "maintenance.auto=false", "-c", "gc.auto=0", "-c", "core.attributesFile=" + os.devnull,
            "-C", str(repo), *args]
    try:
        proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_clean_env(),
                              timeout=45, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConservatorError("git_unavailable", "required Git evidence is unavailable", 5,
                               operation=args[0] if args else "git") from exc
    if proc.returncode and not allow_failure:
        raise ConservatorError("git_evidence_failed", "Git could not prove the requested state", 5,
                               operation=args[0] if args else "git", returncode=proc.returncode)
    return proc.returncode, proc.stdout


def _text(raw: bytes, field: str) -> str:
    try:
        value = raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise ConservatorError("invalid_git_output", f"Git returned invalid {field} evidence", 5) from exc
    if not value:
        raise ConservatorError("missing_git_evidence", f"Git returned no {field} evidence", 5)
    return value


def _absolute(path: str | Path, *, must_exist: bool = True) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    _no_symlink_components(candidate, allow_missing=not must_exist)
    try:
        resolved = candidate.resolve(strict=must_exist)
    except OSError as exc:
        raise ConservatorError("path_unavailable", "configured path cannot be resolved", 5) from exc
    if must_exist and not resolved.exists():
        raise ConservatorError("path_unavailable", "configured path does not exist", 5)
    return resolved


def _no_symlink_components(path: Path, *, allow_missing: bool = False) -> None:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if allow_missing:
                continue
            raise ConservatorError("path_unavailable", "path component is missing", 5)
        except OSError as exc:
            raise ConservatorError("path_unavailable", "path component cannot be inspected", 5) from exc
        if stat.S_ISLNK(info.st_mode):
            raise ConservatorError("symlink_path", "symlinked paths are not accepted", 3)


def _stat_identity(path: Path) -> dict[str, int]:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ConservatorError("identity_unavailable", "filesystem identity is unavailable", 5) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ConservatorError("unsafe_path_type", "worktree path is not a real directory", 3)
    return {"device": info.st_dev, "inode": info.st_ino, "ctime_ns": info.st_ctime_ns,
            "mtime_ns": info.st_mtime_ns}


def _repo_info(repo_arg: str | Path) -> dict[str, Any]:
    requested = Path(repo_arg).expanduser()
    if not requested.is_absolute():
        requested = Path.cwd() / requested
    _no_symlink_components(requested)
    repo = _absolute(requested)
    if not repo.is_dir():
        raise ConservatorError("repo_invalid", "--repo must name a Git worktree directory", 2)
    top = Path(_text(git(repo, "rev-parse", "--show-toplevel"), "repository root")).resolve()
    common = Path(_text(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"), "common Git directory")).resolve()
    gitdir = Path(_text(git(repo, "rev-parse", "--path-format=absolute", "--git-dir"), "Git directory")).resolve()
    common_stat = common.stat()
    return {"repo": str(top), "common_dir": str(common), "common_device": common_stat.st_dev,
            "common_inode": common_stat.st_ino, "git_dir": str(gitdir)}


def _worktrees(repo: Path) -> list[dict[str, Any]]:
    raw = git(repo, "worktree", "list", "--porcelain", "-z")
    rows: list[dict[str, Any]] = []
    record: list[bytes] = []
    for field in raw.split(b"\0"):
        if field:
            record.append(field)
        elif record:
            values: dict[str, Any] = {"locked": False, "prunable": False, "bare": False, "branch": None}
            for item in record:
                key, sep, val = item.partition(b" ")
                if key == b"worktree" and sep:
                    values["path"] = os.fsdecode(val)
                elif key == b"HEAD" and sep:
                    values["head"] = val.decode("ascii", "strict")
                elif key == b"branch" and sep:
                    values["branch"] = val.decode("utf-8", "strict")
                elif key == b"locked":
                    values["locked"] = True
                elif key == b"prunable":
                    values["prunable"] = True
                elif key == b"bare":
                    values["bare"] = True
            if "path" not in values or "head" not in values:
                raise ConservatorError("invalid_worktree_registry", "Git worktree registry is incomplete", 5)
            rows.append(values)
            record = []
    if record:
        raise ConservatorError("invalid_worktree_registry", "Git worktree registry is truncated", 5)
    return rows


def _registered(repo: Path, path: Path) -> dict[str, Any] | None:
    target = str(path)
    matches = [row for row in _worktrees(repo) if os.path.normpath(row["path"]) == target]
    return matches[0] if len(matches) == 1 else None


def _gitdir_identity(path: Path) -> tuple[str, dict[str, int], str]:
    dotgit = path / ".git"
    try:
        info = dotgit.lstat()
    except OSError as exc:
        raise ConservatorError("gitdir_unavailable", "worktree Git metadata is unavailable", 5) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ConservatorError("unsupported_gitdir", "only standard linked-worktree metadata is supported", 3)
    try:
        fd = os.open(dotgit, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise ConservatorError("unsupported_gitdir", "worktree Git pointer is not a regular file", 3)
            pointer = os.read(fd, 4097)
            after = os.fstat(fd)
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                raise ConservatorError("gitdir_changed", "worktree Git pointer changed during inspection", 3)
        finally:
            os.close(fd)
    except OSError as exc:
        raise ConservatorError("gitdir_unavailable", "worktree Git pointer could not be read safely", 5) from exc
    if len(pointer) > 4096 or not pointer.startswith(b"gitdir: "):
        raise ConservatorError("unsupported_gitdir", "worktree Git pointer is malformed", 3)
    try:
        admin = Path(os.fsdecode(pointer[len(b"gitdir: "):].strip())).resolve(strict=True)
    except OSError as exc:
        raise ConservatorError("gitdir_unavailable", "linked-worktree Git metadata is unavailable", 5) from exc
    ai = admin.stat()
    return str(admin), {"device": ai.st_dev, "inode": ai.st_ino}, hashlib.sha256(pointer).hexdigest()


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _overlaps(left: Path, right: Path) -> bool:
    return _inside(left, right) or _inside(right, left)


def _live_process(path: Path) -> tuple[bool | None, str | None]:
    """Return live, absent, or unknown; unknown evidence never authorizes removal."""
    try:
        canonical = str(path.resolve(strict=True))
    except OSError:
        return None, "worktree path cannot be resolved for liveness proof"
    # lsof is the portable cwd inventory. Parse only the exact `fcwd` record
    # for each PID: lsof may also emit transient /proc names, which are not
    # evidence that a process is using the candidate. Do not restrict owners;
    # another user's live cwd must not become eligible through a UID filter.
    lsof = shutil.which("lsof")
    if lsof:
        try:
            result = subprocess.run(
                [lsof, "-nP", "-w", "-a", "-d", "cwd", "-Fpcfn"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20,
                env={"PATH": os.environ.get("PATH", ""), "LC_ALL": "C"}, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return None, "lsof process liveness probe failed"
        if result.returncode not in (0, 1):
            return None, "lsof process liveness evidence is incomplete"
        current_pid: int | None = None
        current_fd_is_cwd = False
        for field in result.stdout.splitlines():
            if field.startswith(b"p"):
                try:
                    current_pid = int(field[1:])
                except ValueError:
                    return None, "lsof returned malformed process identity"
                current_fd_is_cwd = False
                continue
            if field.startswith(b"f"):
                current_fd_is_cwd = field[1:] == b"cwd"
                continue
            if not field.startswith(b"n") or current_pid is None or not current_fd_is_cwd:
                continue
            name = os.fsdecode(field[1:])
            if name == canonical or name.startswith(canonical + os.sep):
                return True, "process cwd is inside worktree"
        # lsof establishes cwd liveness. On Linux, inspect argv separately so
        # a service launched from elsewhere but executing a path in this tree
        # remains active; do not let transient system cwd entries poison it.
        if not Path("/proc").is_dir():
            ps = shutil.which("ps")
            if not ps:
                return None, "process argument inventory unavailable"
            try:
                arguments = subprocess.run([ps, "-axww", "-o", "pid=,command="],
                                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                           timeout=20, env={"PATH": os.environ.get("PATH", ""), "LC_ALL": "C"},
                                           check=False)
            except (OSError, subprocess.TimeoutExpired):
                return None, "process argument inventory failed"
            if arguments.returncode or not arguments.stdout.strip():
                return None, "process argument inventory is incomplete"
            boundary = re.compile(r"(?:^|[\s\"'=])" + re.escape(canonical) + r"(?=$|[\s\"'/])")
            for line in arguments.stdout.splitlines():
                row = re.fullmatch(rb"\s*([0-9]+)\s+(.+)", line)
                if row is None:
                    return None, "process argument inventory is malformed"
                if boundary.search(os.fsdecode(row[2])):
                    return True, "process arguments reference worktree"
            return False, None
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                status = (entry / "status").read_text(errors="replace")
                uid_line = next((line for line in status.splitlines() if line.startswith("Uid:")), "")
                if not uid_line or int(uid_line.split()[1]) != os.getuid():
                    continue
                try:
                    args = (entry / "cmdline").read_bytes().split(b"\0")
                except FileNotFoundError:
                    continue
                except PermissionError:
                    return None, "same-user process arguments are unreadable"
                for arg in args:
                    if not arg:
                        continue
                    value = os.fsdecode(arg)
                    try:
                        resolved = str(Path(value).resolve(strict=True))
                    except (OSError, ValueError):
                        continue
                    if resolved == canonical or resolved.startswith(canonical + os.sep):
                        return True, "process arguments reference worktree"
            except FileNotFoundError:
                continue
            except PermissionError:
                continue
            except (OSError, ValueError, IndexError):
                return None, "same-user process arguments are incomplete"
        return False, None

    if os.name == "posix" and Path("/proc/self/cmdline").exists():
        try:
            entries = list(Path("/proc").iterdir())
        except OSError:
            return None, "process inventory unavailable"
        unknown = False
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                args = (entry / "cmdline").read_bytes().split(b"\0")
            except FileNotFoundError:
                continue
            except PermissionError:
                continue
            except OSError:
                unknown = True
                continue
            for arg in args:
                if not arg:
                    continue
                value = os.fsdecode(arg)
                try:
                    resolved = str(Path(value).resolve(strict=True))
                except (OSError, ValueError):
                    continue
                if resolved == canonical or resolved.startswith(canonical + os.sep):
                    return True, "process arguments reference worktree"
        return False, None
    return None, "no process liveness probe available"


def _head_commit(repo: Path, ref: str) -> str:
    if not ref or ref.startswith("-") or "\0" in ref:
        raise ConservatorError("invalid_base_ref", "base ref must be a non-option Git ref", 2)
    return _text(git(repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"), "base commit")


def _tracked_tree(repo: Path, head: str) -> list[dict[str, Any]]:
    raw = git(repo, "ls-tree", "-r", "-z", "--full-tree", head)
    entries: list[dict[str, Any]] = []
    for row in raw.split(b"\0"):
        if not row:
            continue
        meta, sep, name = row.partition(b"\t")
        fields = meta.split()
        if not sep or len(fields) != 3:
            raise ConservatorError("tree_unavailable", "Git tree listing is malformed", 5)
        mode, kind, oid = fields
        try:
            path = name.decode("utf-8", "strict")
            mode_s = mode.decode("ascii")
            oid_s = oid.decode("ascii")
        except UnicodeDecodeError as exc:
            raise ConservatorError("unsupported_filename", "tracked names must be valid UTF-8", 3) from exc
        if kind != b"blob" or mode_s not in ("100644", "100755"):
            raise ConservatorError("unsupported_tracked_entry", "symlinks, submodules, and special Git entries are refused", 3,
                                   path=path, mode=mode_s)
        if not _safe_member_name(path):
            raise ConservatorError("unsafe_tracked_path", "tracked path cannot be archived safely", 3, path=path)
        entries.append({"path": path, "mode": int(mode_s, 8), "oid": oid_s})
    if len(entries) > MAX_MEMBERS:
        raise ConservatorError("archive_limit", "tracked file count exceeds the safety limit", 4)
    return entries


def _safe_member_name(name: str) -> bool:
    if (not name or "\0" in name or "\\" in name or name.startswith("/") or
            re.match(r"^[A-Za-z]:", name) or "//" in name):
        return False
    normalized = name[:-1] if name.endswith("/") else name
    pure = PurePosixPath(normalized)
    if any(part in ("", ".", "..") for part in pure.parts):
        return False
    if "/".join(pure.parts) != normalized:
        return False
    return pure.parts[0] != ".git" and ".git" not in pure.parts


def _lfs_present(repo: Path, head: str) -> bool:
    pattern = r"filter[[:space:]]*=[[:space:]]*lfs|^version https://git-lfs.github.com/spec/v1"
    rc, _ = git_status(repo, "grep", "-I", "-n", "-E", pattern, head, "--", allow_failure=True)
    if rc == 0:
        return True
    if rc == 1:
        return False
    raise ConservatorError("lfs_evidence_unavailable", "Git LFS state could not be proven", 5)


def _check_snapshot(repo: Path, row: dict[str, Any], repo_info: dict[str, Any], base_ref: str,
                    base_head: str, min_age_hours: float, protected: list[Path]) -> dict[str, Any]:
    path = Path(row["path"])
    result: dict[str, Any] = {"path": str(path), "head": row["head"], "branch": row["branch"],
                              "eligible": False, "reasons": []}
    def refuse(reason: str) -> dict[str, Any]:
        result["reasons"].append(reason)
        return result
    try:
        info = _stat_identity(path)
        if not _inside(path, path.parent):
            return refuse("invalid-path")
        if row.get("prunable"):
            return refuse("prunable-or-incomplete-registration")
        if row.get("locked"):
            return refuse("locked")
        if row.get("bare"):
            return refuse("bare-worktree")
        if path == Path(repo_info["repo"]):
            return refuse("main-worktree")
        if any(_overlaps(path, entry) for entry in protected):
            return refuse("protected-path")
        if any(_overlaps(path, Path(value)) for value in (repo_info["common_dir"],)):
            return refuse("overlaps-git-metadata")
        if time.time() - info["mtime_ns"] / 1e9 < min_age_hours * 3600:
            return refuse("too-young")
        live, live_reason = _live_process(path)
        if live is None:
            return refuse("liveness-unknown" if not live_reason else "liveness-unknown")
        if live:
            return refuse("active")
        admin, admin_identity, pointer_hash = _gitdir_identity(path)
        if not _inside(Path(admin), Path(repo_info["common_dir"]) / "worktrees"):
            return refuse("unrecognized-worktree-metadata")
        registered = _registered(repo, path)
        if not registered or registered["head"] != row["head"]:
            return refuse("registration-changed")
        status = git(path, "status", "--porcelain=v2", "-z", "--untracked-files=all", "--ignored=matching")
        if status:
            records = status.split(b"\0")
            if any(record.startswith(b"! ") for record in records):
                return refuse("precious-ignored-state")
            if any(record.startswith(b"? ") for record in records):
                return refuse("untracked-state")
            return refuse("dirty-state")
        index = git(path, "ls-files", "-v", "-z")
        for entry in index.split(b"\0"):
            if entry and (len(entry) < 3 or entry[:1] != b"H" or entry[1:2] != b" "):
                return refuse("hidden-or-unsupported-index-state")
        tree = _tracked_tree(repo, row["head"])
        modes = git(path, "ls-files", "--stage", "-z")
        for entry in modes.split(b"\0"):
            if not entry:
                continue
            meta, sep, _name = entry.partition(b"\t")
            if not sep or len(meta.split()) != 3:
                return refuse("index-evidence-malformed")
            if meta.split()[0] not in (b"100644", b"100755"):
                return refuse("unsupported-index-entry")
        if git(path, "rev-parse", "--is-shallow-repository").strip() == b"true":
            return refuse("shallow-repository")
        sparse_rc, sparse = git_status(path, "config", "--bool", "core.sparseCheckout", allow_failure=True)
        if sparse_rc not in (0, 1):
            return refuse("sparse-checkout-state-unknown")
        if sparse_rc == 0 and sparse.strip().lower() == b"true":
            return refuse("sparse-checkout")
        if _lfs_present(repo, row["head"]):
            return refuse("git-lfs-state")
        ancestry, _ = git_status(repo, "merge-base", "--is-ancestor", row["head"], base_head, allow_failure=True)
        if ancestry == 1:
            return refuse("not-merged-into-base")
        if ancestry != 0:
            return refuse("base-ancestry-unknown")
        current = git(path, "rev-parse", "HEAD").strip().decode("ascii")
        if current != row["head"]:
            return refuse("head-changed")
        status_hash = hashlib.sha256(status).hexdigest()
        index_hash = hashlib.sha256(index).hexdigest()
        result.update({"eligible": True, "reasons": [], "repo": repo_info["repo"], "common_dir": repo_info["common_dir"],
                       "common_device": repo_info["common_device"], "common_inode": repo_info["common_inode"],
                       "worktree_git_dir": admin, "worktree_git_device": admin_identity["device"],
                       "worktree_git_inode": admin_identity["inode"], "git_pointer_sha256": pointer_hash,
                       "device": info["device"], "inode": info["inode"], "ctime_ns": info["ctime_ns"],
                       "mtime_ns": info["mtime_ns"], "status_sha256": status_hash,
                       "index_sha256": index_hash, "tree_sha256": hashlib.sha256(canonical_json(tree)).hexdigest(),
                       "base_ref": base_ref, "base_head": base_head, "min_age_hours": min_age_hours})
        return result
    except ConservatorError as exc:
        return refuse("evidence-unavailable:" + exc.code)
    except (OSError, ValueError, UnicodeError):
        return refuse("evidence-unavailable")


def scan(repo_arg: str | Path, *, root_arg: str | Path | None, base_ref: str,
         min_age_hours: float, protected_args: Iterable[str | Path] = ()) -> dict[str, Any]:
    if min_age_hours < 0 or not (min_age_hours < float("inf")):
        raise ConservatorError("invalid_age", "minimum age must be finite and non-negative", 2)
    info = _repo_info(repo_arg)
    repo = Path(info["repo"])
    base_head = _head_commit(repo, base_ref)
    root = _absolute(root_arg) if root_arg is not None else None
    if root is not None:
        _no_symlink_components(root)
        if not root.is_dir():
            raise ConservatorError("root_invalid", "--root must be an existing directory", 2)
    protected = [_absolute(p) for p in protected_args]
    protected.append(Path(info["repo"]))
    rows = _worktrees(repo)
    results = []
    for row in rows:
        path = Path(row["path"])
        try:
            path = path.resolve(strict=True)
        except OSError:
            results.append({"path": row["path"], "head": row["head"], "branch": row["branch"],
                            "eligible": False, "reasons": ["worktree-path-unavailable"]})
            continue
        if root is not None and not _inside(path, root):
            continue
        normalized = dict(row, path=str(path))
        results.append(_check_snapshot(repo, normalized, info, base_ref, base_head, min_age_hours, protected))
    results.sort(key=lambda item: item["path"])
    return {"repository": info, "root": str(root) if root else None, "base_ref": base_ref,
            "base_head": base_head, "min_age_hours": min_age_hours,
            "protected_paths": sorted(str(path) for path in protected), "worktrees": results,
            "eligible_count": sum(bool(item["eligible"]) for item in results)}


def make_plan(scan_data: dict[str, Any], archive_dir_arg: str | Path) -> dict[str, Any]:
    archive_dir = _absolute(archive_dir_arg, must_exist=False)
    _no_symlink_components(archive_dir, allow_missing=True)
    repo = Path(scan_data["repository"]["repo"])
    common = Path(scan_data["repository"]["common_dir"])
    if _overlaps(archive_dir, repo) or _overlaps(archive_dir, common):
        raise ConservatorError("unsafe_archive_directory", "archive directory must be outside the repository and Git metadata", 2)
    candidates = [item for item in scan_data["worktrees"] if item["eligible"]]
    for candidate in candidates:
        if _overlaps(archive_dir, Path(candidate["path"])):
            raise ConservatorError("unsafe_archive_directory", "archive directory overlaps a candidate worktree", 2)
    payload = {"schema": PLAN_SCHEMA, "repository": scan_data["repository"], "root": scan_data["root"],
               "base_ref": scan_data["base_ref"], "base_head": scan_data["base_head"],
               "min_age_hours": scan_data["min_age_hours"], "protected_paths": scan_data["protected_paths"],
               "archive_dir": str(archive_dir), "candidates": candidates}
    return {"schema": PLAN_SCHEMA, "payload": payload, "plan_sha256": plan_digest(payload)}


def _validate_plan(value: Any, expected_sha: str, repo_arg: str | Path, archive_arg: str | Path,
                   root_arg: str | Path | None) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"schema", "payload", "plan_sha256"} or value.get("schema") != PLAN_SCHEMA:
        raise ConservatorError("invalid_plan", "plan schema is invalid", 2)
    payload = value["payload"]
    if not isinstance(payload, dict) or set(payload) != {"schema", "repository", "root", "base_ref", "base_head",
            "min_age_hours", "protected_paths", "archive_dir", "candidates"} or payload.get("schema") != PLAN_SCHEMA:
        raise ConservatorError("invalid_plan", "plan payload is invalid", 2)
    actual = plan_digest(payload)
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha or "") or actual != expected_sha or value["plan_sha256"] != actual:
        raise ConservatorError("plan_digest_mismatch", "plan digest does not match the exact reviewed plan", 3,
                               actual_sha256=actual)
    info = _repo_info(repo_arg)
    if info != payload["repository"]:
        raise ConservatorError("repository_identity_changed", "configured repository identity differs from the plan", 3)
    archive = _absolute(archive_arg, must_exist=False)
    if str(archive) != payload["archive_dir"]:
        raise ConservatorError("archive_destination_changed", "archive directory differs from the plan", 3)
    root = str(_absolute(root_arg)) if root_arg is not None else None
    if root != payload["root"]:
        raise ConservatorError("root_changed", "configured worktree root differs from the plan", 3)
    return payload


def _candidate_identity(item: dict[str, Any]) -> dict[str, Any]:
    keys = ("path", "repo", "common_dir", "common_device", "common_inode", "head", "branch", "device", "inode",
            "ctime_ns", "mtime_ns", "worktree_git_dir", "worktree_git_device", "worktree_git_inode",
            "git_pointer_sha256", "status_sha256", "index_sha256", "tree_sha256", "base_ref", "base_head",
            "min_age_hours")
    return {key: item.get(key) for key in keys}


def _append_journal(path: Path, record: dict[str, Any]) -> None:
    _no_symlink_components(path.parent, allow_missing=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.write(fd, canonical_json(record) + b"\n")
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_json(path: Path, value: dict[str, Any], *, replace: bool = True) -> None:
    _no_symlink_components(path.parent, allow_missing=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        if not replace:
            raise ConservatorError("receipt_exists", "receipt path already exists", 3)
        if path.is_symlink() or not path.is_file():
            raise ConservatorError("unsafe_receipt_path", "receipt path is not a regular file", 3)
    fd, temp_name = tempfile.mkstemp(prefix=".wtc-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _archive_path(archive_dir: Path, candidate: dict[str, Any]) -> Path:
    key = hashlib.sha256(os.fsencode(candidate["path"])).hexdigest()[:20]
    return archive_dir / f"{key}-{candidate['head'][:12]}.tar"


def _parse_tree_entry(row: bytes) -> tuple[str, str, str, int]:
    meta, sep, raw_name = row.partition(b"\t")
    fields = meta.split()
    if not sep or len(fields) != 3:
        raise ConservatorError("tree_unavailable", "Git tree listing is malformed", 5)
    try:
        mode = fields[0].decode("ascii")
        kind = fields[1].decode("ascii")
        oid = fields[2].decode("ascii")
        name = raw_name.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise ConservatorError("unsupported_filename", "tracked names must be valid UTF-8", 3) from exc
    return name, oid, mode, int(mode, 8)


def _git_blob_sha(repo: Path, oid: str) -> tuple[int, str]:
    argv = ["git", "-c", "maintenance.auto=false", "-C", str(repo), "cat-file", "blob", oid]
    digest = hashlib.sha256()
    size = 0
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=_clean_env())
        assert proc.stdout is not None
        with proc.stdout:
            while True:
                chunk = proc.stdout.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_MEMBER_BYTES:
                    proc.kill()
                    proc.wait()
                    raise ConservatorError("archive_limit", "tracked file exceeds the safety limit", 4)
                digest.update(chunk)
        if proc.wait(timeout=45) != 0:
            raise ConservatorError("git_evidence_failed", "Git blob content is unavailable", 5)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConservatorError("git_evidence_failed", "Git blob content is unavailable", 5) from exc
    return size, digest.hexdigest()


def _archive_members(archive: Path, repo: Path, head: str | None, *, compare_blobs: bool,
                    require_manifest: bool = True) -> tuple[list[dict[str, Any]], bytes]:
    try:
        size = archive.stat().st_size
        if size > MAX_ARCHIVE_BYTES:
            raise ConservatorError("archive_limit", "archive exceeds the safety limit", 4)
        tree_raw = git(repo, "ls-tree", "-r", "-z", "--full-tree", head) if head is not None else b""
        expected: dict[str, tuple[str, int]] = {}
        for row in tree_raw.split(b"\0"):
            if not row:
                continue
            name, oid, mode, mode_int = _parse_tree_entry(row)
            if not _safe_member_name(name) or mode not in ("100644", "100755"):
                raise ConservatorError("unsupported_tracked_entry", "archive contains unsupported Git entries", 4)
            if name == "WORKTREE_CONSERVATOR.json":
                raise ConservatorError("reserved_archive_path", "tracked path conflicts with archive manifest", 4)
            expected[name] = (oid, mode_int)
        if len(expected) > MAX_MEMBERS:
            raise ConservatorError("archive_limit", "archive member count exceeds the safety limit", 4)
        seen: dict[str, tarfile.TarInfo] = {}
        path_types: dict[str, str] = {}
        actual: list[dict[str, Any]] = []
        manifest_bytes: bytes | None = None
        total = 0
        with tarfile.open(archive, "r:") as tar:
            for number, member in enumerate(tar):
                if number >= MAX_MEMBERS + 2:
                    raise ConservatorError("archive_limit", "archive member count exceeds the safety limit", 4)
                name = member.name
                while name.startswith("./"):
                    name = name[2:]
                if name in ("", ".") and member.isdir():
                    continue
                if not _safe_member_name(name):
                    raise ConservatorError("unsafe_archive_path", "archive contains an unsafe path", 4)
                if name in seen:
                    raise ConservatorError("duplicate_archive_path", "archive contains duplicate paths", 4)
                seen[name] = member
                member_kind = "directory" if member.isdir() else "file"
                path_types[name] = member_kind
                if member.isdir():
                    continue
                if not member.isfile():
                    raise ConservatorError("unsafe_archive_type", "archive contains a symlink or special file", 4)
                if member.size < 0 or member.size > MAX_MEMBER_BYTES:
                    raise ConservatorError("archive_limit", "archive member exceeds the safety limit", 4)
                if name == "WORKTREE_CONSERVATOR.json" and member.size > MAX_MANIFEST_BYTES:
                    raise ConservatorError("archive_limit", "archive manifest exceeds the safety limit", 4)
                total += member.size
                if total > MAX_ARCHIVE_BYTES:
                    raise ConservatorError("archive_limit", "archive expands beyond the safety limit", 4)
                fileobj = tar.extractfile(member)
                if fileobj is None:
                    raise ConservatorError("archive_corrupt", "archive member cannot be read", 4)
                digest = hashlib.sha256()
                read_size = 0
                manifest_chunks = bytearray() if name == "WORKTREE_CONSERVATOR.json" else None
                while True:
                    chunk = fileobj.read(1024 * 1024)
                    if not chunk:
                        break
                    read_size += len(chunk)
                    digest.update(chunk)
                    if manifest_chunks is not None:
                        manifest_chunks.extend(chunk)
                if read_size != member.size:
                    raise ConservatorError("archive_corrupt", "archive member size does not match its header", 4)
                if manifest_chunks is not None:
                    manifest_bytes = bytes(manifest_chunks)
                    continue
                tree_item = expected.get(name) if head is not None else None
                if head is not None and not tree_item:
                    raise ConservatorError("archive_content_mismatch", "archive contains a path absent from the recorded Git tree", 4)
                oid, mode_int = tree_item if tree_item else (None, member.mode & 0o777)
                expected_mode = 0o755 if (mode_int & 0o111) else 0o644
                actual_mode = 0o755 if (member.mode & 0o111) else 0o644
                if actual_mode != expected_mode:
                    raise ConservatorError("archive_content_mismatch", "archive file mode differs from the Git tree", 4)
                if compare_blobs and oid is not None:
                    blob_size, blob_digest = _git_blob_sha(repo, oid)
                    if read_size != blob_size or digest.hexdigest() != blob_digest:
                        raise ConservatorError("archive_content_mismatch", "archived bytes differ from the Git object", 4)
                actual.append({"path": name, "mode": expected_mode, "size": read_size,
                               "sha256": digest.hexdigest(), "git_oid": oid})
        for name, kind in path_types.items():
            parts = PurePosixPath(name).parts
            for index in range(1, len(parts)):
                ancestor = "/".join(parts[:index])
                if path_types.get(ancestor) == "file":
                    raise ConservatorError("archive_path_collision", "archive file is used as a parent directory", 4)
            if kind == "file" and any(other.startswith(name + "/") for other in path_types):
                raise ConservatorError("archive_path_collision", "archive file conflicts with a descendant path", 4)
        if manifest_bytes is None and require_manifest:
            raise ConservatorError("archive_manifest_missing", "archive manifest is missing", 4)
        if head is not None and set(item["path"] for item in actual) != set(expected):
            raise ConservatorError("archive_content_mismatch", "archive file set differs from the Git tree", 4)
        actual.sort(key=lambda item: item["path"])
        return actual, manifest_bytes
    except (tarfile.TarError, OSError) as exc:
        if isinstance(exc, ConservatorError):
            raise
        raise ConservatorError("archive_corrupt", "archive cannot be safely read", 4) from exc


def _add_tar_member(tar: tarfile.TarFile, name: str, data: bytes, mode: int) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mtime = 0
    tar.addfile(info, io.BytesIO(data))


def _create_archive(repo: Path, candidate: dict[str, Any], archive_path: Path) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    if archive_path.exists() or archive_path.is_symlink():
        raise ConservatorError("archive_exists", "archive destination already exists", 3)
    fd, temp_name = tempfile.mkstemp(prefix=".wtc-archive-", dir=archive_path.parent)
    os.close(fd)
    os.unlink(temp_name)
    try:
        argv = ["git", "-c", "core.fsmonitor=false", "-c",
                "maintenance.auto=false", "-c", "gc.auto=0", "-c", "core.attributesFile=" + os.devnull,
                "-C", str(repo), "archive", "--format=tar", candidate["head"]]
        with os.fdopen(os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600), "wb") as out:
            try:
                proc = subprocess.run(argv, stdout=out, stderr=subprocess.PIPE, env=_clean_env(), timeout=180, check=False)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ConservatorError("archive_create_failed", "Git could not create the preservation archive", 4) from exc
            out.flush()
            os.fsync(out.fileno())
            if proc.returncode:
                raise ConservatorError("archive_create_failed", "Git archive command failed", 4,
                                       returncode=proc.returncode)
        staging = Path(temp_name)
        members, _ = _archive_members(staging, repo, candidate["head"], compare_blobs=True,
                                      require_manifest=False)
        manifest = {"schema": MANIFEST_SCHEMA, "source": {"repository": candidate["repo"],
                    "common_dir": candidate["common_dir"], "common_device": candidate["common_device"],
                    "common_inode": candidate["common_inode"], "worktree_path": candidate["path"],
                    "head": candidate["head"], "branch": candidate["branch"]},
                    "policy": {"tracked_regular_files_only": True, "preserves": ["file-bytes", "executable-bit"],
                               "omits": [".git-metadata", "timestamps", "ownership", "xattrs", "ACLs"]},
                    "members": members}
        with tarfile.open(staging, "a:") as tar:
            _add_tar_member(tar, "WORKTREE_CONSERVATOR.json", canonical_json(manifest), 0o644)
        with open(staging, "rb") as archive_file:
            os.fsync(archive_file.fileno())
        final_members, manifest_bytes = _archive_members(staging, repo, candidate["head"], compare_blobs=True)
        if final_members != members or strict_json(manifest_bytes) != manifest:
            raise ConservatorError("archive_verification_failed", "archive manifest verification failed", 4)
        archive_digest = _archive_sha256(staging)
        try:
            os.link(staging, archive_path, follow_symlinks=False)
        except FileExistsError as exc:
            raise ConservatorError("archive_exists", "archive destination already exists", 3) from exc
        os.unlink(staging)
        dfd = os.open(archive_path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
        if _archive_sha256(archive_path) != archive_digest:
            raise ConservatorError("archive_verification_failed", "published archive digest changed", 4)
        return archive_digest, members, manifest
    finally:
        try:
            os.unlink(temp_name)
        except OSError:
            pass


def _repository_lock(common_dir: Path):
    import fcntl
    lock_path = common_dir / "worktree-conservator.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ConservatorError("operation_locked", "another conservator operation holds the repository lock", 3) from exc
        yield fd
    finally:
        os.close(fd)


def _lock_context(common_dir: Path):
    from contextlib import contextmanager
    return contextmanager(_repository_lock)(common_dir)


def apply_plan(plan_file: str | Path, expected_sha: str, repo_arg: str | Path, archive_arg: str | Path,
               root_arg: str | Path | None = None) -> dict[str, Any]:
    plan_path = _absolute(plan_file)
    try:
        value = strict_json(plan_path.read_bytes())
    except OSError as exc:
        raise ConservatorError("plan_unavailable", "plan file cannot be read", 5) from exc
    payload = _validate_plan(value, expected_sha, repo_arg, archive_arg, root_arg)
    repo = Path(payload["repository"]["repo"])
    common = Path(payload["repository"]["common_dir"])
    archive_dir = Path(payload["archive_dir"])
    _no_symlink_components(archive_dir, allow_missing=True)
    for candidate in payload["candidates"]:
        if not isinstance(candidate, dict) or candidate.get("eligible") is not True:
            raise ConservatorError("invalid_plan_candidate", "plan includes an invalid candidate", 2)
    with _lock_context(common):
        current_base = _head_commit(repo, payload["base_ref"])
        if current_base != payload["base_head"]:
            raise ConservatorError("stale_base", "base ref moved after plan creation", 3,
                                   planned_base=payload["base_head"], current_base=current_base)
        fresh = scan(repo, root_arg=payload["root"], base_ref=payload["base_ref"],
                     min_age_hours=payload["min_age_hours"], protected_args=payload["protected_paths"])
        current = {item["path"]: item for item in fresh["worktrees"] if item["eligible"]}
        for candidate in payload["candidates"]:
            actual = current.get(candidate["path"])
            if actual is None or _candidate_identity(actual) != _candidate_identity(candidate):
                raise ConservatorError("stale_candidate", "candidate identity or safety state changed after planning", 3,
                                       path=candidate.get("path"))
        archive_dir.mkdir(parents=True, exist_ok=True)
        journal = archive_dir / "journal.jsonl"
        plan_sha = plan_digest(payload)
        applied: list[dict[str, Any]] = []
        partial = False
        for candidate in payload["candidates"]:
            path = Path(candidate["path"])
            archive = _archive_path(archive_dir, candidate)
            receipt = archive.with_suffix(archive.suffix + ".receipt.json")
            _append_journal(journal, {"schema": "worktree-conservator.journal/v1", "plan_sha256": plan_sha,
                                      "path": str(path), "state": "planned"})
            try:
                digest, members, manifest = _create_archive(repo, candidate, archive)
            except ConservatorError as exc:
                partial = bool(applied)
                raise ConservatorError(exc.code, exc.message, exc.exit_code, partial=partial,
                                       applied=applied, path=str(path)) from exc
            receipt_value = {"schema": "worktree-conservator.receipt/v1", "plan_sha256": plan_sha,
                             "candidate": candidate, "archive": str(archive), "archive_sha256": digest,
                             "manifest_sha256": hashlib.sha256(canonical_json(manifest)).hexdigest(),
                             "state": "archive_verified"}
            _atomic_json(receipt, receipt_value, replace=False)
            _append_journal(journal, {"schema": "worktree-conservator.journal/v1", "plan_sha256": plan_sha,
                                      "path": str(path), "archive": str(archive), "archive_sha256": digest,
                                      "state": "archive_verified"})
            # Recheck after preservation and once more immediately before the non-force Git removal.
            for phase in ("after-preservation", "before-removal"):
                fresh = scan(repo, root_arg=payload["root"], base_ref=payload["base_ref"],
                             min_age_hours=payload["min_age_hours"], protected_args=payload["protected_paths"])
                actual = next((item for item in fresh["worktrees"] if item["path"] == candidate["path"] and item["eligible"]), None)
                if actual is None or _candidate_identity(actual) != _candidate_identity(candidate):
                    receipt_value["state"] = "archived_not_removed"
                    receipt_value["failure"] = "candidate changed " + phase
                    _atomic_json(receipt, receipt_value)
                    _append_journal(journal, {"schema": "worktree-conservator.journal/v1", "plan_sha256": plan_sha,
                                              "path": str(path), "state": "archived_not_removed", "phase": phase})
                    raise ConservatorError("candidate_changed", "candidate changed after archival; archive retained and worktree not intentionally removed", 3,
                                           partial=bool(applied), path=str(path), archive=str(archive), archive_sha256=digest)
            _append_journal(journal, {"schema": "worktree-conservator.journal/v1", "plan_sha256": plan_sha,
                                      "path": str(path), "state": "remove_started"})
            # A final no-follow identity and HEAD check sits directly before Git's non-force removal.
            root_identity = _stat_identity(path)
            if root_identity["device"] != candidate["device"] or root_identity["inode"] != candidate["inode"]:
                receipt_value["state"] = "archived_not_removed"
                receipt_value["failure"] = "device/inode changed immediately before removal"
                _atomic_json(receipt, receipt_value)
                raise ConservatorError("candidate_changed", "candidate filesystem identity changed before removal", 3,
                                       partial=bool(applied), path=str(path), archive=str(archive), archive_sha256=digest)
            if git(path, "rev-parse", "HEAD").strip().decode("ascii") != candidate["head"]:
                receipt_value["state"] = "archived_not_removed"
                receipt_value["failure"] = "HEAD changed immediately before removal"
                _atomic_json(receipt, receipt_value)
                raise ConservatorError("candidate_changed", "candidate HEAD changed before removal", 3,
                                       partial=bool(applied), path=str(path), archive=str(archive), archive_sha256=digest)
            argv = ["git", "-c", "core.fsmonitor=false", "-c",
                    "maintenance.auto=false", "-c", "gc.auto=0", "-C", str(repo), "worktree", "remove", "--", str(path)]
            try:
                removed = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_clean_env(),
                                         timeout=180, check=False)
            except (OSError, subprocess.TimeoutExpired) as exc:
                receipt_value["state"] = "removal_outcome_unknown"
                _atomic_json(receipt, receipt_value)
                raise ConservatorError("removal_outcome_unknown", "Git removal outcome is unknown; keep archive and inspect worktree", 5,
                                       partial=True, path=str(path), archive=str(archive), archive_sha256=digest) from exc
            registered = _registered(repo, path)
            path_absent = not os.path.lexists(path)
            if removed.returncode != 0 or registered is not None or not path_absent:
                receipt_value["state"] = "removal_failed_preserved" if not path_absent else "removal_outcome_unknown"
                receipt_value["remove_returncode"] = removed.returncode
                _atomic_json(receipt, receipt_value)
                raise ConservatorError("removal_not_proven", "non-force Git removal did not prove the requested result", 5,
                                       partial=True, path=str(path), archive=str(archive), archive_sha256=digest)
            receipt_value["state"] = "removed_and_verified"
            receipt_value["remove_returncode"] = 0
            _atomic_json(receipt, receipt_value)
            _append_journal(journal, {"schema": "worktree-conservator.journal/v1", "plan_sha256": plan_sha,
                                      "path": str(path), "archive": str(archive), "archive_sha256": digest,
                                      "state": "removed_and_verified"})
            applied.append({"path": str(path), "head": candidate["head"], "archive": str(archive),
                            "archive_sha256": digest, "receipt": str(receipt)})
        return {"plan_sha256": plan_sha, "applied": applied, "partial": False}


def _archive_sha256(path: Path) -> str:
    """Hash an ordinary bounded archive without loading it into memory."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ConservatorError("archive_corrupt", "archive must be an ordinary file", 4)
        if info.st_size > MAX_ARCHIVE_BYTES:
            raise ConservatorError("archive_limit", "archive exceeds the safety limit", 4)
        digest = hashlib.sha256()
        count = 0
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            count += len(chunk)
            if count > MAX_ARCHIVE_BYTES:
                raise ConservatorError("archive_limit", "archive exceeds the safety limit", 4)
            digest.update(chunk)
        return digest.hexdigest()


def _read_manifest(archive: Path, repo: Path, expected_digest: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not re.fullmatch(r"[0-9a-f]{64}", expected_digest or ""):
        raise ConservatorError("invalid_archive_digest", "a 64-character trusted archive SHA-256 is required", 2)
    try:
        actual = _archive_sha256(archive)
    except OSError as exc:
        raise ConservatorError("archive_unavailable", "archive cannot be read", 5) from exc
    if actual != expected_digest:
        raise ConservatorError("archive_digest_mismatch", "archive digest does not match the trusted SHA-256", 4)
    members, manifest_bytes = _archive_members(archive, repo, None, compare_blobs=False)
    manifest = strict_json(manifest_bytes)
    if not isinstance(manifest, dict) or set(manifest) != {"schema", "source", "policy", "members"} or manifest.get("schema") != MANIFEST_SCHEMA:
        raise ConservatorError("archive_manifest_invalid", "archive manifest schema is invalid", 4)
    manifest_members = manifest.get("members")
    if not isinstance(manifest_members, list) or len(manifest_members) != len(members):
        raise ConservatorError("archive_manifest_mismatch", "archive manifest member list is invalid", 4)
    for observed, recorded in zip(members, manifest_members):
        if not isinstance(recorded, dict) or set(recorded) != {"path", "mode", "size", "sha256", "git_oid"}:
            raise ConservatorError("archive_manifest_mismatch", "archive manifest member schema is invalid", 4)
        if any(observed[key] != recorded[key] for key in ("path", "mode", "size", "sha256")):
            raise ConservatorError("archive_manifest_mismatch", "archive manifest does not describe its exact members", 4)
        if not isinstance(recorded["git_oid"], str) or not re.fullmatch(r"[0-9a-f]{40,64}", recorded["git_oid"]):
            raise ConservatorError("archive_manifest_mismatch", "archive manifest Git object id is invalid", 4)
    members = manifest_members
    return manifest, members


def _read_receipt(receipt_arg: str | Path, archive: Path, expected_digest: str,
                  repo_info: dict[str, Any]) -> dict[str, Any]:
    """Read an apply receipt and bind it to the archive and repository being verified."""
    receipt = _absolute(receipt_arg)
    try:
        value = strict_json(receipt.read_bytes())
    except OSError as exc:
        raise ConservatorError("receipt_unavailable", "receipt cannot be read", 4) from exc
    if not isinstance(value, dict) or value.get("schema") != RECEIPT_SCHEMA:
        raise ConservatorError("receipt_invalid", "receipt schema is invalid", 4)
    required = {"schema", "plan_sha256", "candidate", "archive", "archive_sha256", "manifest_sha256", "state"}
    if not required.issubset(value) or not isinstance(value.get("candidate"), dict):
        raise ConservatorError("receipt_invalid", "receipt is missing required preservation fields", 4)
    candidate = value["candidate"]
    if not isinstance(value.get("archive"), str) or not value["archive"]:
        raise ConservatorError("receipt_invalid", "receipt archive path is missing", 4)
    if value.get("archive_sha256") != expected_digest:
        raise ConservatorError("receipt_digest_mismatch", "receipt archive digest differs from the trusted SHA-256", 4)
    for key in ("plan_sha256", "archive_sha256", "manifest_sha256"):
        if not isinstance(value.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", value[key]):
            raise ConservatorError("receipt_invalid", f"receipt {key} is not a SHA-256 digest", 4)
    if value.get("state") not in {"archive_verified", "archived_not_removed", "removed_and_verified",
                                   "removal_failed_preserved", "removal_outcome_unknown"}:
        raise ConservatorError("receipt_state_invalid", "receipt does not prove a retained verified archive", 4)
    for key in ("repo", "common_dir", "common_device", "common_inode"):
        if candidate.get(key) != repo_info.get(key):
            raise ConservatorError("receipt_repository_mismatch", "receipt repository identity differs from --repo", 4)
    return value


def verify_archive(archive_arg: str | Path, expected_sha: str, repo_arg: str | Path,
                   receipt_arg: str | Path | None = None) -> dict[str, Any]:
    """Verify a retained archive against Git objects and, optionally, its apply receipt.

    This is deliberately read-only. It rechecks the archive digest, manifest, repository
    identity, tracked file set, modes, Git object IDs, and blob bytes. A receipt, when
    supplied, must describe the same archive and identity but the worktree itself may be
    absent after a successful apply.
    """
    archive = _absolute(archive_arg)
    _no_symlink_components(archive)
    info = _repo_info(repo_arg)
    repo = Path(info["repo"])
    manifest, members = _read_manifest(archive, repo, expected_sha)
    source = manifest.get("source")
    if not isinstance(source, dict):
        raise ConservatorError("archive_manifest_invalid", "archive source identity is invalid", 4)
    source_identity = {"repository": info["repo"], "common_dir": info["common_dir"],
                       "common_device": info["common_device"], "common_inode": info["common_inode"]}
    for key, expected in source_identity.items():
        if source.get(key) != expected:
            raise ConservatorError("archive_repository_mismatch", "archive belongs to a different Git repository identity", 4)
    head = source.get("head")
    if not isinstance(head, str) or not re.fullmatch(r"[0-9a-fA-F]{40,64}", head):
        raise ConservatorError("archive_manifest_invalid", "archive HEAD is invalid", 4)
    # Re-read through the Git-aware path. This proves every archived regular file against
    # the recorded commit, including its mode, object ID, and SHA-256 blob bytes.
    checked_members, manifest_bytes = _archive_members(archive, repo, head, compare_blobs=True)
    if checked_members != members:
        raise ConservatorError("archive_content_mismatch", "archive members differ between manifest and Git verification", 4)
    checked_manifest = strict_json(manifest_bytes)
    if checked_manifest != manifest:
        raise ConservatorError("archive_manifest_mismatch", "archive manifest changed during verification", 4)
    manifest_sha = hashlib.sha256(canonical_json(manifest)).hexdigest()
    receipt_value = None
    if receipt_arg is not None:
        receipt_value = _read_receipt(receipt_arg, archive, expected_sha, info)
        if receipt_value.get("manifest_sha256") != manifest_sha:
            raise ConservatorError("receipt_manifest_mismatch", "receipt manifest digest differs from the archive", 4)
    return {"archive": str(archive), "archive_sha256": expected_sha, "manifest_sha256": manifest_sha,
            "repository": info["repo"], "common_dir": info["common_dir"], "head": head,
            "files": len(members), "git_content_verified": True,
            "receipt": str(_absolute(receipt_arg)) if receipt_arg is not None else None,
            "receipt_archive": receipt_value.get("archive") if receipt_value is not None else None,
            "receipt_state": receipt_value.get("state") if receipt_value is not None else None}


def _audit_digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ConservatorError("audit_invalid_digest", f"audit {field} is not a SHA-256 digest", 4)
    return value


def _audit_path(value: Any, field: str, *, must_exist: bool = False) -> Path:
    if not isinstance(value, str) or not value:
        raise ConservatorError("audit_invalid_path", f"audit {field} path is missing", 4)
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise ConservatorError("audit_invalid_path", f"audit {field} path must be absolute", 4)
    return _absolute(candidate, must_exist=must_exist)


def _audit_plan(plan_arg: str | Path, expected_sha: str, repo_arg: str | Path,
                archive_dir: Path) -> tuple[str, dict[str, Any]]:
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha or ""):
        raise ConservatorError("plan_digest_mismatch", "a trusted plan SHA-256 is required for audit", 3)
    plan_path = _absolute(plan_arg)
    try:
        value = strict_json(plan_path.read_bytes())
    except OSError as exc:
        raise ConservatorError("plan_unavailable", "plan file cannot be read", 5) from exc
    if not isinstance(value, dict) or set(value) != {"schema", "payload", "plan_sha256"} or value.get("schema") != PLAN_SCHEMA:
        raise ConservatorError("invalid_plan", "plan schema is invalid", 2)
    payload = value.get("payload")
    required = {"schema", "repository", "root", "base_ref", "base_head", "min_age_hours",
                "protected_paths", "archive_dir", "candidates"}
    if not isinstance(payload, dict) or set(payload) != required or payload.get("schema") != PLAN_SCHEMA:
        raise ConservatorError("invalid_plan", "plan payload is invalid", 2)
    actual = plan_digest(payload)
    if actual != expected_sha or value.get("plan_sha256") != actual:
        raise ConservatorError("plan_digest_mismatch", "plan digest does not match the exact reviewed plan", 3,
                               actual_sha256=actual)
    info = _repo_info(repo_arg)
    if payload.get("repository") != info:
        raise ConservatorError("repository_identity_changed", "configured repository identity differs from the plan", 3)
    if payload.get("archive_dir") != str(archive_dir):
        raise ConservatorError("archive_destination_changed", "audit archive directory differs from the plan", 3)
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or any(not isinstance(item, dict) or item.get("eligible") is not True for item in candidates):
        raise ConservatorError("invalid_plan", "plan candidates are invalid", 2)
    return actual, {"path": str(plan_path), "payload": payload, "candidates": candidates}


def _audit_journal(journal: Path, archive_dir: Path) -> tuple[dict[tuple[str, str], list[dict[str, Any]]], list[dict[str, Any]]]:
    try:
        raw = journal.read_bytes()
    except OSError as exc:
        raise ConservatorError("journal_unavailable", "journal cannot be read", 4) from exc
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ConservatorError("journal_limit", "journal exceeds the safety limit", 4)
    if not raw.strip():
        raise ConservatorError("journal_empty", "journal contains no operation records", 4)
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    records: list[dict[str, Any]] = []
    allowed_states = {"planned", "archive_verified", "archived_not_removed", "remove_started", "removed_and_verified"}
    for line_no, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            raise ConservatorError("journal_invalid", "journal contains a blank record", 4, line=line_no)
        try:
            record = strict_json(line)
        except ConservatorError as exc:
            raise ConservatorError("journal_invalid", "journal record is malformed", 4, line=line_no) from exc
        if not isinstance(record, dict) or record.get("schema") != JOURNAL_SCHEMA:
            raise ConservatorError("journal_invalid", "journal record schema is invalid", 4, line=line_no)
        required = {"schema", "plan_sha256", "path", "state"}
        allowed = required | {"archive", "archive_sha256"} | ({"phase"} if record.get("state") == "archived_not_removed" else set())
        if not required.issubset(record) or set(record) - allowed:
            raise ConservatorError("journal_invalid", "journal record fields are invalid", 4, line=line_no)
        plan_sha = _audit_digest(record.get("plan_sha256"), "journal plan_sha256")
        path = _audit_path(record.get("path"), "journal worktree", must_exist=False)
        state = record.get("state")
        if state not in allowed_states:
            raise ConservatorError("journal_invalid", "journal state is unsupported", 4, line=line_no)
        has_archive = "archive" in record or "archive_sha256" in record
        if state == "planned" and has_archive:
            raise ConservatorError("journal_invalid", "planned journal record cannot bind an archive", 4, line=line_no)
        archive = None
        archive_sha = None
        if state != "planned":
            allowed_fields = required | {"archive", "archive_sha256"}
            if state in {"archived_not_removed", "remove_started"}:
                optional = {"phase"} if state == "archived_not_removed" else set()
                if set(record) not in (required | optional, required | optional | {"archive", "archive_sha256"}):
                    raise ConservatorError("journal_invalid", "journal archive record fields are invalid", 4, line=line_no)
            elif set(record) != allowed_fields:
                raise ConservatorError("journal_invalid", "journal archive record fields are invalid", 4, line=line_no)
            if has_archive:
                archive = _audit_path(record.get("archive"), "journal archive", must_exist=False)
                try:
                    archive.relative_to(archive_dir)
                except ValueError as exc:
                    raise ConservatorError("journal_archive_outside", "journal archive is outside the audit directory", 4,
                                           line=line_no) from exc
                archive_sha = _audit_digest(record.get("archive_sha256"), "journal archive_sha256")
        normalized = {"line": line_no, "schema": JOURNAL_SCHEMA, "plan_sha256": plan_sha,
                      "path": str(path), "state": state}
        if archive is not None:
            normalized.update({"archive": str(archive), "archive_sha256": archive_sha})
        key = (plan_sha, str(path))
        grouped.setdefault(key, []).append(normalized)
        records.append(normalized)
    order = {"planned": 0, "archive_verified": 1, "archived_not_removed": 2,
             "remove_started": 3, "removed_and_verified": 4}
    for key, entries in grouped.items():
        states = [entry["state"] for entry in entries]
        if states != sorted(states, key=order.get) or len(states) != len(set(states)):
            raise ConservatorError("journal_transition_invalid", "journal state transition is not monotonic", 4,
                                   plan_sha256=key[0], path=key[1])
        bound_archive = next((entry.get("archive") for entry in entries if entry.get("archive")), None)
        bound_archive_sha = next((entry.get("archive_sha256") for entry in entries if entry.get("archive_sha256")), None)
        for entry in entries:
            if entry.get("archive") not in (None, bound_archive) or entry.get("archive_sha256") not in (None, bound_archive_sha):
                raise ConservatorError("journal_archive_mismatch", "journal archive binding changed during an operation", 4,
                                       plan_sha256=key[0], path=key[1])
    return grouped, records


def _audit_receipt(receipt_path: Path, archive_dir: Path, repo_arg: str | Path,
                   repo_info: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        value = strict_json(receipt_path.read_bytes())
    except OSError as exc:
        raise ConservatorError("receipt_unavailable", "receipt cannot be read", 4,
                               receipt=str(receipt_path)) from exc
    if not isinstance(value, dict) or value.get("schema") != RECEIPT_SCHEMA:
        raise ConservatorError("receipt_invalid", "receipt schema is invalid", 4, receipt=str(receipt_path))
    required = {"schema", "plan_sha256", "candidate", "archive", "archive_sha256", "manifest_sha256", "state"}
    if not required.issubset(value) or not isinstance(value.get("candidate"), dict):
        raise ConservatorError("receipt_invalid", "receipt is missing required preservation fields", 4,
                               receipt=str(receipt_path))
    archive = _audit_path(value.get("archive"), "receipt archive", must_exist=True)
    try:
        archive.relative_to(archive_dir)
    except ValueError as exc:
        raise ConservatorError("receipt_archive_outside", "receipt archive is outside the audit directory", 4,
                               receipt=str(receipt_path)) from exc
    if archive != receipt_path.with_name(receipt_path.name.removesuffix(".receipt.json")):
        raise ConservatorError("receipt_archive_mismatch", "receipt filename does not bind to its archive", 4,
                               receipt=str(receipt_path))
    candidate = value["candidate"]
    candidate_path = _audit_path(candidate.get("path"), "receipt candidate", must_exist=False)
    plan_sha = _audit_digest(value.get("plan_sha256"), "receipt plan_sha256")
    archive_sha = _audit_digest(value.get("archive_sha256"), "receipt archive_sha256")
    manifest_sha = _audit_digest(value.get("manifest_sha256"), "receipt manifest_sha256")
    state = value.get("state")
    if state not in {"archive_verified", "archived_not_removed", "removed_and_verified",
                     "removal_failed_preserved", "removal_outcome_unknown"}:
        raise ConservatorError("receipt_state_invalid", "receipt does not prove a retained verified archive", 4,
                               receipt=str(receipt_path))
    # This binds the receipt, archive, manifest, and Git objects before any lifecycle
    # inference.  A failure is a hard audit failure rather than an attention warning.
    verified = verify_archive(archive, archive_sha, repo_arg, receipt_path)
    if verified["manifest_sha256"] != manifest_sha:
        raise ConservatorError("receipt_manifest_mismatch", "receipt manifest digest differs from the archive", 4,
                               receipt=str(receipt_path))
    return value, {"receipt": str(receipt_path), "archive": str(archive), "archive_sha256": archive_sha,
                   "manifest_sha256": manifest_sha, "plan_sha256": plan_sha, "candidate_path": str(candidate_path),
                   "candidate": candidate, "state": state, "verified": verified}


def audit_archive_dir(archive_dir_arg: str | Path, repo_arg: str | Path,
                      plan_arg: str | Path | None = None, plan_sha256: str | None = None) -> dict[str, Any]:
    """Reconcile retained archives, receipts, and journal state without mutation.

    Audit is an independent lifecycle readback: every discovered archive must have a
    receipt, every receipt must verify against Git, and journal transitions must match
    the receipt's claimed state.  A preserved or incomplete operation is reported as
    attention rather than upgraded to removal success.
    """
    archive_dir = _absolute(archive_dir_arg)
    if not archive_dir.is_dir():
        raise ConservatorError("archive_directory_invalid", "audit archive directory must be an existing directory", 2)
    _no_symlink_components(archive_dir)
    info = _repo_info(repo_arg)
    repo = Path(info["repo"])
    if _overlaps(archive_dir, repo) or _overlaps(archive_dir, Path(info["common_dir"])):
        raise ConservatorError("unsafe_archive_directory", "audit archive directory overlaps the repository or Git metadata", 3)
    journal = archive_dir / "journal.jsonl"
    if not journal.exists() or not journal.is_file():
        raise ConservatorError("journal_missing", "audit archive directory has no readable recovery journal", 4,
                               journal=str(journal))
    _no_symlink_components(journal, allow_missing=False)
    grouped, _records = _audit_journal(journal, archive_dir)
    plan_info = None
    bound_plan_sha = None
    plan_candidates: dict[tuple[str, str], dict[str, Any]] = {}
    if plan_arg is not None or plan_sha256 is not None:
        if plan_arg is None or plan_sha256 is None:
            raise ConservatorError("plan_binding_incomplete", "audit plan binding requires both --plan and --plan-sha256", 2)
        bound_plan_sha, plan_info = _audit_plan(plan_arg, plan_sha256.lower(), repo_arg, archive_dir)
        for candidate in plan_info["candidates"]:
            candidate_path = _audit_path(candidate.get("path"), "plan candidate", must_exist=False)
            plan_candidates[(bound_plan_sha, str(candidate_path))] = candidate
    receipts: dict[str, dict[str, Any]] = {}
    receipt_paths: list[Path] = []
    archive_paths: list[Path] = []
    for entry in sorted(archive_dir.iterdir(), key=lambda item: item.name):
        if entry.name == "journal.jsonl":
            continue
        if entry.name.endswith(".tar"):
            _no_symlink_components(entry)
            if not entry.is_file():
                raise ConservatorError("archive_invalid", "audit archive is not an ordinary file", 4,
                                       archive=str(entry))
            archive_paths.append(entry.resolve())
        elif entry.name.endswith(".tar.receipt.json"):
            _no_symlink_components(entry)
            if not entry.is_file():
                raise ConservatorError("receipt_invalid", "audit receipt is not an ordinary file", 4,
                                       receipt=str(entry))
            receipt_paths.append(entry.resolve())
    for receipt_path in receipt_paths:
        value, summary = _audit_receipt(receipt_path, archive_dir, repo_arg, info)
        archive = summary["archive"]
        if archive in receipts:
            raise ConservatorError("receipt_duplicate", "multiple receipts bind the same archive", 4, archive=archive)
        receipts[archive] = summary
    archive_set = {str(path.resolve()) for path in archive_paths}
    for archive in archive_set:
        if archive not in receipts:
            raise ConservatorError("archive_receipt_missing", "retained archive has no matching receipt", 4,
                                   archive=archive)
    for archive, summary in receipts.items():
        key = (summary["plan_sha256"], summary["candidate_path"])
        entries = grouped.get(key)
        if not entries:
            raise ConservatorError("journal_receipt_mismatch", "receipt has no matching journal operation", 4,
                                   archive=archive, path=summary["candidate_path"])
        final_state = entries[-1]["state"]
        state = summary["state"]
        if state == "removed_and_verified" and final_state != "removed_and_verified":
            raise ConservatorError("journal_receipt_mismatch", "receipt removal state is absent from the journal", 4,
                                   archive=archive, path=summary["candidate_path"])
        if final_state == "removed_and_verified" and state != "removed_and_verified":
            raise ConservatorError("journal_receipt_mismatch", "journal proves removal but receipt does not", 4,
                                   archive=archive, path=summary["candidate_path"])
        if bound_plan_sha is not None:
            if summary["plan_sha256"] != bound_plan_sha or key not in plan_candidates:
                raise ConservatorError("plan_receipt_mismatch", "receipt candidate is absent from the bound plan", 4,
                                       archive=archive, path=summary["candidate_path"])
            if _candidate_identity(summary["candidate"]) != _candidate_identity(plan_candidates[key]):
                raise ConservatorError("plan_receipt_mismatch", "receipt candidate identity differs from the bound plan", 4,
                                       archive=archive, path=summary["candidate_path"])
    operations: list[dict[str, Any]] = []
    attention: list[dict[str, Any]] = []
    for key, entries in sorted(grouped.items(), key=lambda item: item[0]):
        plan_sha, path = key
        summary = next((item for item in receipts.values() if item["plan_sha256"] == plan_sha and item["candidate_path"] == path), None)
        states = [entry["state"] for entry in entries]
        archive = entries[-1].get("archive")
        if summary is None:
            operation = {"plan_sha256": plan_sha, "path": path, "journal_states": states,
                         "archive": archive, "lifecycle": "planned_unfinished" if states == ["planned"] else "journal_incomplete",
                         "archive_verified": False, "worktree_present": os.path.lexists(path),
                         "registered": _registered(repo, Path(path)) is not None}
            operations.append(operation)
            attention.append({"path": path, "plan_sha256": plan_sha, "lifecycle": operation["lifecycle"]})
            if states != ["planned"]:
                raise ConservatorError("journal_receipt_missing", "journal archive phase has no receipt", 4,
                                       path=path, plan_sha256=plan_sha)
            continue
        registered = _registered(repo, Path(path)) if os.path.lexists(path) else None
        worktree_present = os.path.lexists(path)
        state = summary["state"]
        if state == "removed_and_verified":
            if worktree_present or registered is not None:
                raise ConservatorError("removal_not_proven", "receipt claims removal but the worktree still exists", 5,
                                       path=path, archive=summary["archive"])
            lifecycle = "removed_and_verified"
        elif state == "archive_verified" and states == ["planned", "archive_verified"] and worktree_present and registered is not None:
            lifecycle = "archive_preserved"
        elif state in {"archive_verified", "archived_not_removed", "removal_failed_preserved"}:
            lifecycle = "archive_only_unresolved" if not worktree_present and registered is None else "archive_preserved"
        else:
            lifecycle = "removal_outcome_unknown"
        operation = {"plan_sha256": plan_sha, "path": path, "journal_states": states,
                     "archive": summary["archive"], "archive_sha256": summary["archive_sha256"],
                     "receipt": summary["receipt"], "receipt_state": state, "lifecycle": lifecycle,
                     "archive_verified": True, "worktree_present": worktree_present,
                     "registered": registered is not None}
        operations.append(operation)
        if lifecycle != "removed_and_verified":
            attention.append({"path": path, "plan_sha256": plan_sha, "lifecycle": lifecycle,
                              "receipt_state": state, "journal_states": states})
    if bound_plan_sha is not None:
        for key, candidate in sorted(plan_candidates.items()):
            if key not in grouped:
                attention.append({"path": key[1], "plan_sha256": key[0], "lifecycle": "never_started"})
    counts = {"journal_operations": len(grouped), "receipt_count": len(receipts),
              "archive_count": len(archive_paths), "removed_and_verified": sum(item["lifecycle"] == "removed_and_verified" for item in operations),
              "attention": len(attention)}
    return {"schema": AUDIT_SCHEMA, "archive_dir": str(archive_dir), "journal": str(journal),
            "plan": plan_info["path"] if plan_info is not None else None,
            "plan_sha256": bound_plan_sha, "operations": operations, "attention": attention, "counts": counts,
            "complete": not attention}


def _extract_member(tar: tarfile.TarFile, member: tarfile.TarInfo, root: Path) -> None:
    name = member.name
    while name.startswith("./"):
        name = name[2:]
    if name == "WORKTREE_CONSERVATOR.json":
        return
    if not _safe_member_name(name):
        raise ConservatorError("unsafe_archive_path", "archive contains an unsafe restore path", 4)
    target = root.joinpath(*PurePosixPath(name).parts)
    parent = target.parent
    relative = parent.relative_to(root)
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        try:
            info = cursor.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ConservatorError("restore_path_collision", "archive path collides with a non-directory", 4)
        except FileNotFoundError:
            os.mkdir(cursor, 0o755)
    if member.isdir():
        try:
            os.mkdir(target, 0o755)
        except FileExistsError:
            info = target.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ConservatorError("restore_path_collision", "archive directory collides with another entry", 4)
        return
    if not member.isfile() or (member.mode & 0o111 not in (0, 0o111)):
        raise ConservatorError("unsafe_archive_type", "only ordinary 0644/0755 files can be restored", 4)
    source = tar.extractfile(member)
    if source is None:
        raise ConservatorError("archive_corrupt", "archive member cannot be read", 4)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as output:
            shutil.copyfileobj(source, output, 1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
            os.fchmod(output.fileno(), 0o755 if member.mode & 0o111 else 0o644)
    except Exception:
        raise


def restore(archive_arg: str | Path, expected_sha: str, repo_arg: str | Path, target_arg: str | Path) -> dict[str, Any]:
    """Pin validated bytes in a private snapshot before any recovery writes."""
    archive = _absolute(archive_arg)
    _no_symlink_components(archive)
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha or ""):
        raise ConservatorError("invalid_archive_digest", "a trusted archive SHA-256 is required", 2)
    with tempfile.TemporaryDirectory(prefix="worktree-conservator-restore-") as raw:
        snapshot = Path(raw).resolve() / "archive.tar"
        try:
            fd = os.open(archive, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(fd, "rb") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise ConservatorError("archive_corrupt", "archive must be an ordinary file", 4)
                if info.st_size > MAX_ARCHIVE_BYTES:
                    raise ConservatorError("archive_limit", "archive exceeds the safety limit", 4)
                out_fd = os.open(snapshot, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(out_fd, "wb") as output:
                    count = 0
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        count += len(chunk)
                        if count > MAX_ARCHIVE_BYTES:
                            raise ConservatorError("archive_limit", "archive exceeds the safety limit", 4)
                        output.write(chunk)
            # Digest validation, member validation, and extraction use this same private snapshot.
            return _restore_snapshot(snapshot, expected_sha, repo_arg, target_arg)
        except OSError as exc:
            raise ConservatorError("archive_unavailable", "archive cannot be safely snapshotted", 5) from exc


def _restore_snapshot(archive_arg: str | Path, expected_sha: str, repo_arg: str | Path, target_arg: str | Path) -> dict[str, Any]:
    archive = _absolute(archive_arg)
    _no_symlink_components(archive)
    info = _repo_info(repo_arg)
    repo = Path(info["repo"])
    target_given = Path(target_arg).expanduser()
    if not target_given.is_absolute():
        target_given = Path.cwd() / target_given
    target = Path(os.path.abspath(target_given))
    if os.path.lexists(target):
        raise ConservatorError("restore_target_exists", "restore refuses to overwrite an existing path", 3)
    _no_symlink_components(target.parent)
    if not target.parent.is_dir():
        raise ConservatorError("restore_parent_missing", "restore target parent must already exist", 2)
    manifest, members = _read_manifest(archive, repo, expected_sha)
    source = manifest.get("source")
    if not isinstance(source, dict) or source.get("common_dir") != info["common_dir"] or source.get("common_device") != info["common_device"] or source.get("common_inode") != info["common_inode"]:
        raise ConservatorError("restore_repository_mismatch", "archive belongs to a different Git repository identity", 3)
    head = source.get("head")
    if not isinstance(head, str) or not re.fullmatch(r"[0-9a-fA-F]{40,64}", head):
        raise ConservatorError("archive_manifest_invalid", "archive HEAD is invalid", 4)
    tree = _tracked_tree(repo, head)
    expected_paths = {entry["path"] for entry in tree}
    if expected_paths != {entry["path"] for entry in members}:
        raise ConservatorError("archive_content_mismatch", "archive member set differs from the recorded commit", 4)
    for member in members:
        entry = next((item for item in tree if item["path"] == member["path"]), None)
        expected_mode = 0o755 if (entry["mode"] & 0o111) else 0o644
        if not entry or entry["oid"] != member["git_oid"] or expected_mode != member["mode"]:
            raise ConservatorError("archive_content_mismatch", "archive manifest differs from the recorded commit", 4)
        size, digest = _git_blob_sha(repo, entry["oid"])
        if size != member["size"] or digest != member["sha256"]:
            raise ConservatorError("archive_content_mismatch", "archive bytes differ from the recorded Git object", 4)
    common = Path(info["common_dir"])
    with _lock_context(common):
        if os.path.lexists(target):
            raise ConservatorError("restore_target_exists", "restore target appeared before creation", 3)
        if _overlaps(target, repo) or _overlaps(target, common):
            raise ConservatorError("unsafe_restore_target", "restore target overlaps the repository or Git metadata", 3)
        argv = ["git", "-c", "core.fsmonitor=false", "-c",
                "maintenance.auto=false", "-c", "gc.auto=0", "-C", str(repo), "worktree", "add", "--detach",
                "--no-checkout", "--", str(target), head]
        result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_clean_env(), timeout=180, check=False)
        if result.returncode:
            raise ConservatorError("restore_git_add_failed", "Git could not register an empty recovery worktree", 5)
        root_info = _stat_identity(target)
        try:
            with tarfile.open(archive, "r:") as tar:
                for member in tar:
                    _extract_member(tar, member, target)
        except ConservatorError:
            raise
        except (OSError, tarfile.TarError) as exc:
            raise ConservatorError("restore_partial", "archive extraction failed; partial worktree is retained for recovery", 4,
                                   partial=True, target=str(target)) from exc
        if _stat_identity(target)["inode"] != root_info["inode"] or _stat_identity(target)["device"] != root_info["device"]:
            raise ConservatorError("restore_identity_changed", "restore target identity changed during extraction", 3,
                                   partial=True, target=str(target))
        # This updates only the new worktree index; the verified archived bytes are not overwritten.
        reset = subprocess.run(["git", "-c", "core.fsmonitor=false",
                                "-C", str(target), "reset", "--mixed", head], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, env=_clean_env(), timeout=90, check=False)
        if reset.returncode:
            raise ConservatorError("restore_partial", "restored files exist but Git index setup failed; partial worktree is retained", 5,
                                   partial=True, target=str(target))
        status = git(target, "status", "--porcelain=v2", "-z", "--untracked-files=all", "--ignored=matching")
        if status:
            raise ConservatorError("restore_verification_failed", "restored worktree is not clean; partial state is retained", 4,
                                   partial=True, target=str(target))
        if git(target, "rev-parse", "HEAD").strip().decode("ascii") != head:
            raise ConservatorError("restore_verification_failed", "restored HEAD differs from archive", 4,
                                   partial=True, target=str(target))
        if _registered(repo, target) is None:
            raise ConservatorError("restore_verification_failed", "restored registration is absent; partial state is retained", 4,
                                   partial=True, target=str(target))
        return {"target": str(target), "head": head, "archive_sha256": expected_sha, "files": len(members),
                "registered": True, "clean": True}
