from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any
import hashlib
import io
import os
import subprocess
import tarfile
import tempfile

from .core import (
    ConservatorError, MANIFEST_SCHEMA, MAX_ARCHIVE_BYTES, MAX_MANIFEST_BYTES,
    MAX_MEMBER_BYTES, MAX_MEMBERS, _archive_sha256, _clean_env, _git_blob_sha,
    _parse_tree_entry, _safe_member_name, canonical_json, git, strict_json,
)

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
