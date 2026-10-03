import json
from pathlib import Path

from worktree_conservator.demo import run_demo


FIXTURE = Path(__file__).parent / "../conformance/agent-systems-lab.json"


def test_manifest_pins_native_owner_and_shared_adapter():
    manifest = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert manifest["schema"] == "agent-systems-lab-worktree-conformance/v1"
    assert manifest["owner"] == "worktree-conservator"
    assert manifest["native_schema"] == "worktree-conservator.result/v1"
    assert manifest["shared_adapter"]["schema"] == "agent-proof/interop/v1"
    assert len(manifest["cases"]) == 5
    assert manifest["privacy"]["real_worktrees_touched"] is False


def test_disposable_lifecycle_proves_native_readback_stages():
    result = run_demo()
    assert result["demo"] == "worktree-conservator/v1"
    assert result["planned"] == 1
    assert result["applied"] == 1
    assert result["verified"] is True
    assert result["audited"] is True
    assert result["audit_attention"] == 0
    assert result["restored"] is True
    assert isinstance(result["head"], str) and result["head"]
