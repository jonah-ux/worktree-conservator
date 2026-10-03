# Changelog

All notable changes are recorded here. Version 0.2.0 adds independent archive readback and version 0.3.0 adds lifecycle reconciliation. These are early releases with the explicit limits documented in `docs/limitations.md`.

Entries describe their tagged source; GitHub Releases records publication status.

## [0.3.0] - 2026-10-03 (prerelease)

- Added read-only `audit` to reconcile every archive, receipt, and journal transition in an archive directory, with optional exact-plan binding and explicit attention for preserved or unfinished operations.
- Added bounded private archive snapshots so validation and restore consume the same captured bytes, with explicit archive, member, manifest, and member-count limits.
- Added a preservation desk, architecture guidance, and a disposable maintainer recovery example; the source distribution includes these guides and its conformance fixtures.
- Made conformance assertions part of the native unittest gate and required exact unsafe-path refusal before restore writes.
- Verify source-distribution resources and installed module locations in CI, with wheel and sdist consumers run outside the checkout.

## [0.2.0] - 2026-10-01

- Added read-only `verify` to bind an apply receipt to a retained archive and recheck its manifest, repository identity, Git object IDs, file modes, and blob bytes.
- Extended the disposable demo and adversarial fixtures to cover post-apply verification, tampered archives, receipt mismatches, and cross-repository refusal.

## [0.1.0] - 2026-09-30 (prerelease)

- Initial standalone Python 3.11+ stdlib CLI.
- Added read-only `scan`, exact immutable `plan`, proof-gated `apply`, and collision-safe `restore` commands.
- Added canonical JSON result/plan/archive schemas, stable exit classes, deterministic digests, fsynced receipts and recovery journal.
- Added refusal policy for dirty/untracked/ignored/locked/active/unmerged/unknown/unsupported states.
- Added temporary-repository demo, adversarial fixture tests, CI, packaging, documentation, MIT license, and provenance note.

[0.1.0]: https://github.com/jonah-ux/worktree-conservator/releases/tag/v0.1.0
[0.2.0]: https://github.com/jonah-ux/worktree-conservator/releases/tag/v0.2.0
[0.3.0]: https://github.com/jonah-ux/worktree-conservator/releases/tag/v0.3.0
