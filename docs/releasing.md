# Releases

Use semantic versions. Before 1.0, minor versions may change the public interface; patch versions preserve documented commands and JSON schemas. Each release has an annotated Git tag, changelog entry, source distribution, wheel, and SHA-256 checksums.

1. Update the version in `pyproject.toml` and the package together with `CHANGELOG.md`. Review changed acceptance paths, archive parsing, identity checks, and recovery behavior.
2. Run synthetic fixture tests on supported systems. Verify that wrong digests, changed worktrees, unsafe archives, and existing recovery targets preserve user state.
3. Build with `python -m build --sdist --wheel`. Install each distribution in a fresh virtual environment and invoke `worktree-conservator --version`, `--help`, and `demo --json` without a source-path override. Confirm the demo includes the independent `verify` readback.
4. Review the packaged file list and scan source and retained history for secrets. Keep native worktrees, archives, receipts, credentials, environments, and reports outside the source tree.
5. Create an annotated tag for the reviewed commit: `git tag -a vX.Y.Z -m 'Worktree Conservator X.Y.Z'`. Push through the repository's approved release route.
6. Publish a GitHub release containing the wheel, source distribution, and `SHA256SUMS`. Include compatibility notes and tested platforms. Mark experimental versions as prereleases.
7. Download a published artifact, verify its checksum, install it in a fresh environment, and run the installed synthetic demo. Confirm that its JSON records planning, verified removal, independent archive readback, and clean registered recovery.

A GitHub release does not imply PyPI publication. The README uses an exact Git-tag installation route.
