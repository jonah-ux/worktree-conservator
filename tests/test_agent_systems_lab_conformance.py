import json
from pathlib import Path
import unittest

from worktree_conservator.demo import run_demo


FIXTURE = Path(__file__).parent / "../conformance/agent-systems-lab.json"


class AgentSystemsLabConformanceTests(unittest.TestCase):
    def test_manifest_pins_native_owner_and_shared_adapter(self):
        manifest = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema"], "agent-systems-lab-worktree-conformance/v1")
        self.assertEqual(manifest["owner"], "worktree-conservator")
        self.assertEqual(manifest["native_schema"], "worktree-conservator.result/v1")
        self.assertEqual(manifest["shared_adapter"]["schema"], "agent-proof/interop/v1")
        self.assertEqual(len(manifest["cases"]), 5)
        self.assertFalse(manifest["privacy"]["real_worktrees_touched"])
        self.assertTrue(manifest["privacy"]["demo_uses_temporary_repository"])
        self.assertFalse(manifest["privacy"]["credentials_serialized"])

    def test_disposable_lifecycle_proves_native_readback_stages(self):
        result = run_demo()
        self.assertEqual(result["demo"], "worktree-conservator/v1")
        self.assertEqual(result["planned"], 1)
        self.assertEqual(result["applied"], 1)
        self.assertTrue(result["verified"])
        self.assertTrue(result["audited"])
        self.assertEqual(result["audit_attention"], 0)
        self.assertTrue(result["restored"])
        self.assertIsInstance(result["head"], str)
        self.assertTrue(result["head"])
