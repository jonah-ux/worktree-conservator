# Releases

Use semantic versions. Before 1.0, minor versions may change the public interface; patch versions preserve documented commands and JSON schemas. Each release has an annotated Git tag, changelog entry, source distribution, wheel, and SHA-256 checksums.

1. Update the version in `pyproject.toml` and the package together with `CHANGELOG.md`. Review changed acceptance paths, archive parsing, identity checks, and recovery behavior.
2. Run synthetic fixture tests on supported systems. Verify that wrong digests, changed worktrees, unsafe archives, and existing recovery targets preserve user state.
3. Build with `python -m build --sdist --wheel`. The source distribution must include the preservation desk, conformance JSON, and maintainer recovery example. Install each distribution in a fresh virtual environment. Change to a directory outside the checkout, unset `PYTHONPATH`, and invoke `worktree-conservator --version`, `--help`, and `demo --json`. Read back `worktree_conservator.__file__` and require it to be inside that virtual environment. The demo must report successful independent `verify`, `audit`, and `restore`, with `audit_attention: 0`.
4. Review the packaged file list and scan source and retained history for secrets. Keep native worktrees, archives, receipts, credentials, environments, and reports outside the source tree. Compute `SHA256SUMS` from the exact wheel/sdist that passed the installed checks; do not rebuild them between verification and upload.
5. Merge the reviewed candidate through the governed merge owner. Verify the landed source and passing CI, then create an annotated tag at the exact currently advertised `origin/main` commit. In the repository owner's environment, `git` resolves to the external `agent-git-guard` wrapper; its approved tag route is `git push origin refs/tags/vX.Y.Z:refs/tags/vX.Y.Z`. That wrapper checks the allowed public repository, canonical matching HTTPS URLs, annotated tag, secret scan, exact current main, and absent remote tag. Ordinary Git does not supply those policy checks; other maintainers must verify the same facts explicitly. Stop on a refusal rather than replacing a published tag or bypassing the wrapper.
6. Publish a GitHub release containing the tested wheel, source distribution, and `SHA256SUMS`. Include compatibility notes, actual tested platforms, and the exact source commit. Mark this early release as a prerelease. Preserve all existing tags and assets.
7. Download both published artifacts and `SHA256SUMS`, independently verify their hashes, and install each in a new environment outside the checkout. Repeat version/help, import-location readback, and the synthetic lifecycle demo. Confirm planning, verified removal, independent archive verification, lifecycle audit with zero attention, and clean registered recovery before reporting release completion.

A GitHub release does not imply PyPI publication. The README uses an exact Git-tag installation route.

## Publication commands

Replace the version and `REVIEWED_MAIN_COMMIT` with the verified release values.
Build and consumer verification above happen before these commands. On Linux,
generate checksums with `sha256sum`; on macOS use `shasum -a 256` instead.

```console
(cd dist && sha256sum *.whl *.tar.gz > SHA256SUMS)
git tag -a vX.Y.Z REVIEWED_MAIN_COMMIT -m 'Worktree Conservator X.Y.Z'
git cat-file -t refs/tags/vX.Y.Z
git rev-parse 'refs/tags/vX.Y.Z^{}'
git push origin refs/tags/vX.Y.Z:refs/tags/vX.Y.Z
gh release create vX.Y.Z dist/*.whl dist/*.tar.gz dist/SHA256SUMS \
  --repo jonah-ux/worktree-conservator --verify-tag --prerelease \
  --title 'Worktree Conservator X.Y.Z' --notes-file /path/to/reviewed-release-notes.md
```

Require `git cat-file` to print `tag` and the peeled commit to match the reviewed
landed main. Use release notes prepared outside the source tree. After upload,
read back the public tag target, prerelease state, asset digests, and independently
downloaded consumer results. CI builds and checks distributions; publication is an
explicit maintainer action, not an automatic workflow.
