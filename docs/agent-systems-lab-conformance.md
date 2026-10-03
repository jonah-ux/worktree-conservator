# Agent Systems Lab worktree conformance

Worktree Conservator remains the owner of its preserve-first worktree lifecycle
and `worktree-conservator.result/v1` command envelope. This fixture and test make
the disposable lifecycle explicit without touching a real repository or importing
Agent Proof at runtime.

The owner demo proves scan, immutable plan, apply, archive verification, audit,
and restore. The manifest also names native refusal cases for stale plans,
candidate changes, and unsafe archives; the existing owner test suite exercises
those refusal codes. The Agent Proof URL and revision are downstream-owner and
generic-schema provenance metadata only; they do not claim that a Worktree
Conservator adapter or normalization behavior was proven at that pinned revision.
No second registry is created here.

Run the focused test from a fresh checkout. All repository and worktree changes
are made inside temporary fixture directories.

```console
PYTHONPATH=src python3 -m unittest discover -s tests -p test_agent_systems_lab_conformance.py -v
```
