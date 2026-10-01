# Source provenance

Worktree Conservator adapts preservation, worktree identity, non-force removal, and recovery ideas from Jonah's existing internal Git tooling into a standalone CLI. The internal source was pinned and reviewed during extraction; its broader private history is not part of this repository.

The public history records the portable implementation, real correctness changes, synthetic fixture tests, packaging, and release work. No empty commits, fabricated development dates, private worktrees, or personal payloads are included. The owner releases this portable source under the MIT license.

## License provenance

Provenance: independently authored standalone implementation informed by
inspection of jonah-ux/fleet commit 8d6040d8b9710b8da2090bbab94ad088137bed4d,
including runtime/kits/worktree-conservator, runtime/bin/worktree-gc,
and runtime/lib/worktree_removal_contract.py. No Fleet source files were copied.
