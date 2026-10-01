# Contributing

Thanks for helping make destructive Git operations more conservative. Bug reports and pull requests should include the exact command, sanitized JSON output, OS/Python/Git versions, and a disposable fixture reproducing the issue. Never include private repository content or credentials.

## Local checks

- Python 3.11 or newer; Git 2.38 or newer.
- Run `python3 -m unittest discover -s tests -v` and `python3 -m worktree_conservator demo`.
- Verify package artifacts with `python -m build` and install the wheel into a fresh virtual environment.
- Add adversarial refusal tests for every new acceptance path. Use only temporary test repositories; tests must never target home directories, user worktrees, or remote repositories.

## Change expectations

Keep the runtime dependency-free. Do not add implicit repository discovery, network/provider clients, automatic background cleanup, scheduler hooks, force removal, recursive deletion, plan regeneration during apply, or restore-over-existing behavior. All behavior changes must update README and `docs/limitations.md`. Preserve compatibility of the documented result and plan schema or introduce a new schema version.

## Security-sensitive changes

Archive parsing, path traversal, symlink handling, Git invocation, candidate identity, and restore collision checks require a focused threat review. Include positive tests and refusal tests, and explain what evidence is still unknown. Do not claim a lock eliminates races with unrelated programs.
