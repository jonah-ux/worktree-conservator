# Limitations and recovery notes

The `0.3.0` prerelease intentionally supports a narrow, conservative state: linked worktrees containing a clean ordinary Git tree, with an available commit and non-shallow repository. It refuses rather than guesses when state cannot be proven.

Supported archive fidelity is tracked regular-file bytes and executable mode. Not preserved: uncommitted changes, the index as a user artifact, Git admin files, branch names as mutable refs, file timestamps, ownership, xattrs, ACLs, hardlinks, symlinks, submodules, LFS payloads, sparse checkout configuration, or ignored/generated content. A restore is a clean detached worktree at the recorded commit, not a reconstruction of a dirty session.

A plan is a reviewed integrity handle, not authentication. The exact plan digest and candidate identity must match. The base ref is pinned at plan creation; if it moves, apply refuses. The archive SHA-256 must be supplied separately to restore. The digest does not authenticate who supplied an archive.

The tool uses Git's non-force `worktree remove`; it never calls `rm -rf`, `git clean`, `git reset --hard`, or force removal on a user worktree. If archival succeeds but a candidate changes, removal fails, or the outcome is ambiguous, the original path and archive remain. Receipts and `journal.jsonl` record the phase. Do not automatically retry an outcome-unknown operation until the path and Git registry are inspected.

Restoration refuses an existing target and symlinked ancestors. A failed extraction leaves a partial, registered recovery target instead of deleting it. An operator may inspect and resolve that state manually with ordinary Git tools.

The cooperative repository lock serializes this CLI's operations but cannot fence arbitrary external writers. Stop processes using the worktree, avoid concurrent filesystem changes, and review JSON output before apply.

`audit` is a read-only reconciliation of artifacts already written by `apply`; it does not repair a journal, recreate a receipt, remove a worktree, or infer a missing transition. Every archive must remain beside its receipt, and the repository's Git objects must remain available. A journal that is truncated, reordered, or missing a receipt is an integrity refusal. When an operation is preserved or unfinished, the audit result keeps it in `attention` and `complete` remains false.
