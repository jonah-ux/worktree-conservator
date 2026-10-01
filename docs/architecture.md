# Architecture

Worktree Conservator is a refusal-first lifecycle with explicit snapshots:

```text
registered Git worktrees
          |
          v
   scan + liveness + Git/filesystem evidence
          |
          v
   immutable plan + plan_sha256
          |
          v
   apply recheck + verified archive
          |
          v
   non-force removal + receipt
          |
          +--> independent archive/readback verification
          +--> restore only into an absent target
```

## Ownership boundaries

- **Discovery and scan** (`scan`) enumerate only explicitly scoped linked worktrees and collect Git, filesystem, status, age, lock, and liveness evidence. Unknown evidence is a refusal reason.
- **Planning** (`make_plan`, `plan_digest`) turns the reviewed candidate set into a canonical payload bound to repository identity, base ref, worktree identity, and safety snapshots.
- **Apply** (`apply_plan`) rechecks every identity and safety fact before archive creation, writes a verified archive and receipt, then invokes non-force `git worktree remove`. It retains recovery artifacts when any postcondition is uncertain.
- **Archive/readback** (`_archive_members`, `verify_archive`) compares archive bytes to Git blobs, records exact member metadata, and independently verifies the archive against the receipt and source repository.
- **Restore** (`restore`) validates the complete bounded archive first, creates only an absent detached worktree, writes files with no-follow/exclusive semantics, and verifies clean status and HEAD.
- **CLI** (`cli.py`) emits stable JSON envelopes and exit codes around these domain operations. It does not bypass the safety model.

`core.py` is large because these functions encode ordered safety checks around a destructive boundary. Future extraction should follow these ownership boundaries and preserve error codes; splitting by line count alone would make the refusal proof harder to review.

## Invariants

1. The main worktree, dirty state, ignored/untracked files, locks, active processes, unknown evidence, and unmerged commits are never eligible.
2. A plan is bound to exact repository, base, worktree, filesystem, Git, and status identities.
3. A changed plan or stale snapshot refuses before mutation.
4. Archive creation and verification precede removal; removal is non-force.
5. A successful result requires both filesystem absence and Git registration absence.
6. Restore never overwrites an existing target and never follows symlinks.
7. Archive receipts preserve explicit partial/unknown outcomes; an archive is not a replacement for Git history.
8. Concurrent hostile writers remain outside the tool's guarantee; operators must stop them before apply.

## Failure model

Unreadable Git/filesystem/process evidence, stale refs, path identity changes, symlinked components, traversal names, unsupported Git states, malformed archives, digest mismatch, archive corruption, restore collision, and incomplete postconditions fail closed. Exit code 5 means the mutation outcome is not proven; it does not mean removal occurred.

## Non-goals

Worktree Conservator does not force-delete worktrees, preserve uncommitted changes, replace Git history, stop unrelated writers, or promise race-free behavior against a hostile process with access to the same filesystem.
