# Changelog

All notable changes are recorded here. The project follows a small, human-reviewed release process; version 0.1.0 is a pending root-owned public release.

## [0.1.0] - Unreleased

- Initial standalone Python 3.11+ stdlib CLI.
- Added read-only `scan`, exact immutable `plan`, proof-gated `apply`, and collision-safe `restore` commands.
- Added canonical JSON result/plan/archive schemas, stable exit classes, deterministic digests, fsynced receipts and recovery journal.
- Added refusal policy for dirty/untracked/ignored/locked/active/unmerged/unknown/unsupported states.
- Added temporary-repository demo, adversarial fixture tests, CI, packaging, documentation, MIT license, and provenance note.

[0.1.0]: https://github.com/jonah-ux/worktree-conservator/releases/tag/v0.1.0
