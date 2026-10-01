# CLI reference — 0.3

The executable is `worktree-conservator`; the module entry point is `python -m worktree_conservator`. Operational commands return JSON. Help and version use plain text. `--json` is accepted for explicit machine-mode usage.

| Command | Required options | Optional scope |
| --- | --- | --- |
| `scan` | `--repo REPO` | `--root`, `--base-ref`, `--min-age-hours`, repeated `--protected` |
| `plan` | `--repo`, `--archive-dir`, `--output` | Same scope as scan |
| `apply` | `--repo`, `--plan`, `--plan-sha256`, `--archive-dir` | `--root` must match the reviewed plan's scope |
| `restore` | `--repo`, `--archive`, `--archive-sha256`, `--target` | Absent target under an existing canonical parent |
| `verify` | `--repo`, `--archive`, `--archive-sha256` | `--receipt` |
| `audit` | `--repo`, `--archive-dir` | `--plan`, `--plan-sha256` |
| `demo` | None | Temporary synthetic Git repository workflow |

The base-ref default is `origin/main`; the minimum-age default is 24 hours. Base evidence uses the local ref, and Git objects must remain available for recovery. Paths with symlinked components are refused. The target for recovery is a new directory; repository identity and trusted archive digest bind recovery to the recorded data.

## Protocol

The result envelope is `worktree-conservator.result/v1` with `command`, `ok`, `data`, `warnings`, and `errors`. Error items have stable `code` and `message` fields plus recovery details when applicable. Plans use `worktree-conservator.plan/v1`, `payload`, and `plan_sha256`. SHA-256 binds canonical JSON with a versioned domain prefix; reformatting JSON does not change its semantic digest.

Exit 0 indicates the requested operation completed. Exit 2 is invalid input/configuration; 3 is a stale or protected target; 4 is archive/recovery integrity refusal; 5 is unavailable evidence or an unproven mutation outcome. A successful scan can contain protected/refused worktrees. Its `eligible_count` and per-worktree reasons describe that result.

Apply returns the verified archive path and digest, removal results, and receipts. A retained archive can coexist with an unremoved worktree after refusal. `verify` checks one archive independently. `audit` reads the archive directory's journal, receipts, and archives as one lifecycle: it verifies every archive against Git, rejects missing or mismatched bindings, and reports preserved, unfinished, or removed operations. Supplying the exact reviewed plan binds the audit to the planned candidate set. Restore validates a private snapshot, recorded Git blobs, new target identity, clean status, HEAD, and registration; partial recovery remains available when postconditions fail.

The archive format contains tracked ordinary files and a source/member manifest. It does not contain repository history or preserve uncommitted state, xattrs, ownership, or unsupported Git features. The default workflow is observational; apply and restore are explicit filesystem mutations.
