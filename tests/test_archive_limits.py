import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from worktree_conservator import core


class ArchiveLimits(unittest.TestCase):
    def test_oversized_restore_is_refused_before_reading_or_creating_target(self):
        with tempfile.TemporaryDirectory(prefix="wtc-limit-") as name:
            root = Path(name).resolve()
            archive = root / "oversized.tar"
            archive.write_bytes(b"x" * 32)
            target = root / "target"
            with mock.patch.object(core, "MAX_ARCHIVE_BYTES", 16), \
                    mock.patch.object(Path, "read_bytes", side_effect=AssertionError("unbounded read")):
                with self.assertRaises(core.ConservatorError) as caught:
                    core.restore(archive, "0" * 64, root / "not-a-repo", target)
            self.assertEqual(caught.exception.code, "archive_limit")
            self.assertFalse(target.exists())

    def test_archive_hash_streams_and_matches_exact_bytes(self):
        with tempfile.TemporaryDirectory(prefix="wtc-hash-") as name:
            archive = Path(name).resolve() / "archive.tar"
            content = b"bounded archive fixture" * 100
            archive.write_bytes(content)
            with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("unbounded read")):
                self.assertEqual(core._archive_sha256(archive), hashlib.sha256(content).hexdigest())
