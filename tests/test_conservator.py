# Worktree Conservator fixture tests
from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from worktree_conservator import core

from worktree_conservator.core import (ConservatorError, _live_process, apply_plan, make_plan, plan_digest,
                                       restore, scan, verify_archive)


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="wtc-fixture-")
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / "repo"
        self.worktrees = self.root / "worktrees"
        self.worktrees.mkdir()
        self.archive_dir = self.root / "archives"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
        self.git("add", "tracked.txt")
        self.git("commit", "-qm", "base")
        self.head = self.out("rev-parse", "HEAD")
        self.git("update-ref", "refs/remotes/origin/main", self.head)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def git(self, *args: str, cwd: Path | None = None) -> None:
        subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                        "-c", "core.fsmonitor=false", "-C",
                        str(cwd or self.repo), *args], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    def out(self, *args: str, cwd: Path | None = None) -> str:
        return subprocess.check_output(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                                        "-c", "core.fsmonitor=false", "-C",
                                        str(cwd or self.repo), *args], text=True).strip()

    def candidate(self, name: str = "old") -> Path:
        path = self.worktrees / name
        self.git("worktree", "add", "-q", "--detach", str(path), self.head)
        old = path.stat().st_mtime - 48 * 3600
        os.utime(path, (old, old))
        return path

    def plan(self, candidate: Path | None = None) -> tuple[dict, Path]:
        self.candidate() if candidate is None else candidate
        found = scan(self.repo, root_arg=self.worktrees, base_ref="origin/main", min_age_hours=0)
        value = make_plan(found, self.archive_dir)
        path = self.root / "plan.json"
        path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        return value, path

    def test_scan_is_read_only_and_eligible_clean_worktree_is_reported(self) -> None:
        candidate = self.candidate()
        before = self.out("worktree", "list", "--porcelain")
        result = scan(self.repo, root_arg=self.worktrees, base_ref="origin/main", min_age_hours=0)
        self.assertEqual(result["eligible_count"], 1)
        self.assertEqual(result["worktrees"][0]["path"], str(candidate))
        self.assertEqual(before, self.out("worktree", "list", "--porcelain"))
        self.assertTrue(candidate.exists())

    def test_dirty_untracked_ignored_locked_and_active_worktrees_are_refused(self) -> None:
        dirty = self.candidate("dirty")
        (dirty / "tracked.txt").write_text("changed\n", encoding="utf-8")
        untracked = self.candidate("untracked")
        (untracked / "new.txt").write_text("new\n", encoding="utf-8")
        ignored = self.candidate("ignored")
        (ignored / ".gitignore").write_text("secret\n", encoding="utf-8")
        self.git("add", ".gitignore", cwd=ignored)
        self.git("commit", "-qm", "ignore", cwd=ignored)
        (ignored / "secret").write_text("precious\n", encoding="utf-8")
        locked = self.candidate("locked")
        self.git("worktree", "lock", str(locked), "--reason", "fixture")
        active = self.candidate("active")
        with mock.patch("worktree_conservator.core._live_process",
                        side_effect=lambda path: (path.name == "active", "fixture holder" if path.name == "active" else None)):
            result = scan(self.repo, root_arg=self.worktrees, base_ref="origin/main", min_age_hours=0)
        reasons = {Path(item["path"]).name: item["reasons"] for item in result["worktrees"]}
        self.assertIn("dirty-state", reasons["dirty"])
        self.assertIn("untracked-state", reasons["untracked"])
        self.assertIn("precious-ignored-state", reasons["ignored"])
        self.assertIn("locked", reasons["locked"])
        self.assertIn("active", reasons["active"])

    def test_plan_digest_changes_for_any_candidate_edit_and_apply_rejects_stale_plan(self) -> None:
        value, path = self.plan()
        altered = json.loads(path.read_text())
        altered["payload"]["candidates"][0]["inode"] += 1
        self.assertNotEqual(plan_digest(value["payload"]), plan_digest(altered["payload"]))
        path.write_text(json.dumps(altered, sort_keys=True), encoding="utf-8")
        with self.assertRaises(ConservatorError) as caught:
            apply_plan(path, value["plan_sha256"], self.repo, self.archive_dir, self.worktrees)
        self.assertEqual(caught.exception.code, "plan_digest_mismatch")

    def test_base_move_after_plan_refuses_without_touching_worktree(self) -> None:
        candidate = self.candidate()
        value, path = self.plan(candidate)
        (self.repo / "move.txt").write_text("base moved\n", encoding="utf-8")
        self.git("add", "move.txt")
        self.git("commit", "-qm", "move base")
        self.git("update-ref", "refs/remotes/origin/main", self.out("rev-parse", "HEAD"))
        with self.assertRaises(ConservatorError) as caught:
            apply_plan(path, value["plan_sha256"], self.repo, self.archive_dir, self.worktrees)
        self.assertEqual(caught.exception.code, "stale_base")
        self.assertTrue(candidate.exists())

    def test_real_apply_archives_verifies_and_removes_without_force(self) -> None:
        candidate = self.candidate()
        value, path = self.plan(candidate)
        result = apply_plan(path, value["plan_sha256"], self.repo, self.archive_dir, self.worktrees)
        self.assertEqual(len(result["applied"]), 1)
        archived = result["applied"][0]
        self.assertFalse(candidate.exists())
        self.assertEqual(self.out("worktree", "list", "--porcelain").count(str(candidate)), 0)
        self.assertEqual(hashlib.sha256(Path(archived["archive"]).read_bytes()).hexdigest(), archived["archive_sha256"])
        self.assertTrue(Path(archived["receipt"]).exists())
        restored = restore(archived["archive"], archived["archive_sha256"], self.repo, self.worktrees / "restored")
        self.assertTrue(restored["clean"])
        self.assertEqual((self.worktrees / "restored" / "tracked.txt").read_text(), "tracked\n")
        self.assertEqual((self.worktrees / "restored" / "tracked.txt").stat().st_mode & 0o777, 0o644)

    def test_verify_readback_binds_archive_receipt_and_git_content(self) -> None:
        candidate = self.candidate()
        value, path = self.plan(candidate)
        applied = apply_plan(path, value["plan_sha256"], self.repo, self.archive_dir, self.worktrees)
        archived = applied["applied"][0]

        verified = verify_archive(archived["archive"], archived["archive_sha256"], self.repo, archived["receipt"])

        self.assertTrue(verified["git_content_verified"])
        self.assertEqual(verified["files"], 1)
        self.assertEqual(verified["receipt_state"], "removed_and_verified")
        self.assertEqual(verified["head"], self.head)

    def test_verify_rejects_wrong_digest_tampered_archive_and_receipt_mismatch(self) -> None:
        candidate = self.candidate()
        value, path = self.plan(candidate)
        applied = apply_plan(path, value["plan_sha256"], self.repo, self.archive_dir, self.worktrees)
        archived = applied["applied"][0]

        with self.assertRaises(ConservatorError) as caught:
            verify_archive(archived["archive"], "0" * 64, self.repo, archived["receipt"])
        self.assertEqual(caught.exception.code, "archive_digest_mismatch")

        tampered = self.root / "tampered.tar"
        tampered.write_bytes(b"not a tar archive")
        tampered_digest = hashlib.sha256(tampered.read_bytes()).hexdigest()
        with self.assertRaises(ConservatorError) as caught:
            verify_archive(tampered, tampered_digest, self.repo)
        self.assertIn(caught.exception.code, {"archive_corrupt", "archive_manifest_missing"})

        receipt = Path(archived["receipt"])
        receipt_value = json.loads(receipt.read_text(encoding="utf-8"))
        receipt_value["archive_sha256"] = "0" * 64
        mismatched_receipt = self.root / "mismatched.receipt.json"
        mismatched_receipt.write_text(json.dumps(receipt_value), encoding="utf-8")
        with self.assertRaises(ConservatorError) as caught:
            verify_archive(archived["archive"], archived["archive_sha256"], self.repo, mismatched_receipt)
        self.assertEqual(caught.exception.code, "receipt_digest_mismatch")

    def test_verify_rejects_archive_from_a_different_repository(self) -> None:
        candidate = self.candidate()
        value, path = self.plan(candidate)
        applied = apply_plan(path, value["plan_sha256"], self.repo, self.archive_dir, self.worktrees)
        archived = applied["applied"][0]

        other = self.root / "other-repo"
        other.mkdir()
        subprocess.run(["git", "-C", str(other), "init", "-q", "-b", "main"], check=True)
        subprocess.run(["git", "-C", str(other), "config", "user.name", "Fixture"], check=True)
        subprocess.run(["git", "-C", str(other), "config", "user.email", "fixture@example.invalid"], check=True)
        (other / "other.txt").write_text("other\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(other), "add", "other.txt"], check=True)
        subprocess.run(["git", "-C", str(other), "commit", "-qm", "other"], check=True)

        with self.assertRaises(ConservatorError) as caught:
            verify_archive(archived["archive"], archived["archive_sha256"], other)
        self.assertEqual(caught.exception.code, "archive_repository_mismatch")

    def test_restore_uses_verified_snapshot_when_input_is_replaced(self) -> None:
        candidate = self.candidate()
        value, plan_path = self.plan(candidate)
        applied = apply_plan(plan_path, value["plan_sha256"], self.repo, self.archive_dir, self.worktrees)
        archived = applied["applied"][0]
        original = Path(archived["archive"])
        read_manifest = core._read_manifest

        def replace_after_validation(snapshot, repo, digest):
            result = read_manifest(snapshot, repo, digest)
            original.write_bytes(b"a concurrent writer replaced the input archive")
            return result

        target = self.worktrees / "snapshot-recovered"
        with mock.patch.object(core, "_read_manifest", side_effect=replace_after_validation):
            result = restore(original, archived["archive_sha256"], self.repo, target)
        self.assertTrue(result["registered"])
        self.assertEqual((target / "tracked.txt").read_text(), "tracked\n")

    def test_restore_missing_registration_is_not_reported_as_success(self) -> None:
        candidate = self.candidate()
        value, plan_path = self.plan(candidate)
        applied = apply_plan(plan_path, value["plan_sha256"], self.repo, self.archive_dir, self.worktrees)
        archived = applied["applied"][0]
        target = self.worktrees / "registration-refused"
        with mock.patch.object(core, "_registered", return_value=None):
            with self.assertRaises(ConservatorError) as caught:
                restore(archived["archive"], archived["archive_sha256"], self.repo, target)
        self.assertEqual(caught.exception.code, "restore_verification_failed")
        self.assertTrue(caught.exception.details["partial"])
        self.assertEqual((target / "tracked.txt").read_text(), "tracked\n")

    def test_candidate_change_after_plan_is_preserved(self) -> None:
        candidate = self.candidate()
        value, path = self.plan(candidate)
        (candidate / "wip.txt").write_text("user work\n", encoding="utf-8")
        with self.assertRaises(ConservatorError) as caught:
            apply_plan(path, value["plan_sha256"], self.repo, self.archive_dir, self.worktrees)
        self.assertEqual(caught.exception.code, "stale_candidate")
        self.assertTrue(candidate.exists())

    def test_restore_refuses_collision_and_symlink_parent(self) -> None:
        candidate = self.candidate()
        value, path = self.plan(candidate)
        applied = apply_plan(path, value["plan_sha256"], self.repo, self.archive_dir, self.worktrees)
        archived = applied["applied"][0]
        collision = self.worktrees / "collision"
        collision.mkdir()
        with self.assertRaises(ConservatorError) as caught:
            restore(archived["archive"], archived["archive_sha256"], self.repo, collision)
        self.assertEqual(caught.exception.code, "restore_target_exists")
        link_parent = self.root / "link-parent"
        link_parent.symlink_to(self.worktrees, target_is_directory=True)
        with self.assertRaises(ConservatorError) as caught:
            restore(archived["archive"], archived["archive_sha256"], self.repo, link_parent / "recovered")
        self.assertIn(caught.exception.code, {"symlink_path", "path_unavailable"})

    def test_restore_rejects_digest_and_malformed_traversal_archive(self) -> None:
        candidate = self.candidate()
        value, path = self.plan(candidate)
        applied = apply_plan(path, value["plan_sha256"], self.repo, self.archive_dir, self.worktrees)
        archived = applied["applied"][0]
        with self.assertRaises(ConservatorError) as caught:
            restore(archived["archive"], "0" * 64, self.repo, self.worktrees / "bad-digest")
        self.assertEqual(caught.exception.code, "archive_digest_mismatch")
        malicious = self.root / "malicious.tar"
        with tarfile.open(malicious, "w") as tar:
            info = tarfile.TarInfo("../outside.txt")
            info.size = 3
            tar.addfile(info, io.BytesIO(b"bad"))
        with self.assertRaises(ConservatorError) as caught:
            restore(malicious, hashlib.sha256(malicious.read_bytes()).hexdigest(), self.repo, self.worktrees / "unsafe")
        self.assertIn(caught.exception.code, {"unsafe_archive_path", "archive_manifest_missing", "archive_corrupt"})
        self.assertFalse((self.root / "outside.txt").exists())

    def test_git_evidence_failure_is_not_clean(self) -> None:
        candidate = self.candidate()
        with mock.patch("worktree_conservator.core.git", side_effect=ConservatorError("git_unavailable", "no Git", 5)):
            with self.assertRaises(ConservatorError) as caught:
                scan(self.repo, root_arg=self.worktrees, base_ref="origin/main", min_age_hours=0)
        self.assertEqual(caught.exception.code, "git_unavailable")
        self.assertTrue(candidate.exists())

    def test_liveness_probe_matches_exact_cwd_not_prefix_sibling(self) -> None:
        candidate = self.root / "live candidate"
        sibling = self.root / "live candidate-extra"
        candidate.mkdir()
        sibling.mkdir()
        holder = subprocess.Popen(["python3", "-c", "import time; time.sleep(30)"], cwd=sibling)
        try:
            self.assertEqual(_live_process(candidate)[0], False)
        finally:
            holder.terminate()
            holder.wait(timeout=10)

        holder = subprocess.Popen(["python3", "-c", "import time; time.sleep(30)"], cwd=candidate)
        try:
            self.assertEqual(_live_process(candidate)[0], True)
        finally:
            holder.terminate()
            holder.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
