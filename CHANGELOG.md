# Changelog

All notable changes are recorded here. The project follows a small, human-reviewed release process; version 0.2.0 adds independent archive readback and version 0.3.0 adds lifecycle reconciliation.

## [0.3.0] - Unreleased

- Added read-only `audit` to reconcile every archive, receipt, and journal transition in an archive directory, with optional exact-plan binding and explicit attention for preserved or unfinished operations.

## [0.2.0] - Released

- Added read-only `verify` to bind an apply receipt to a retained archive and recheck its manifest, repository identity, Git object IDs, file modes, and blob bytes.
- Extended the disposable demo and adversarial fixtures to cover post-apply verification, tampered archives, receipt mismatches, and cross-repository refusal.

## [0.1.0] - Unreleased

- Initial standalone Python 3.11+ stdlib CLI.
- Added read-only `scan`, exact immutable `plan`, proof-gated `apply`, and collision-safe `restore` commands.
- Added canonical JSON result/plan/archive schemas, stable exit classes, deterministic digests, fsynced receipts and recovery journal.
- Added refusal policy for dirty/untracked/ignored/locked/active/unmerged/unknown/unsupported states.
- Added temporary-repository demo, adversarial fixture tests, CI, packaging, documentation, MIT license, and provenance note.

[0.1.0]: https://github.com/jonah-ux/worktree-conservator/releases/tag/v0.1.0
