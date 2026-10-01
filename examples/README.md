# Maintainer recovery example

`maintainer_recovery.py` runs the complete disposable repository workflow: inventory, exact plan, preserve-first apply, independent archive verification, and restore into a new linked worktree. It uses temporary repositories and never touches a user's worktree.

```console
python3 examples/maintainer_recovery.py
```
