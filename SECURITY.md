# Security policy

Report vulnerabilities privately to the repository owner through the contact method configured on the public repository. Do not open an issue containing secrets, real worktree contents, internal host paths, or an exploit against a live checkout. The latest published release is `worktree-conservator@0.2.0`; the default branch currently carries unreleased `0.3.0` development changes. Report the exact tag or commit when describing an affected version.

## Scope and threat model

The tool handles locally selected paths and potentially untrusted archives. Its guarantees apply to cooperating invocations and ordinary local filesystem/Git behavior. An attacker able to concurrently mutate the same paths as the user, alter Git objects/configuration outside the tool, replace the running executable, or subvert the kernel can defeat local checks. The advisory repo lock does not exclude unrelated writers.

The archive digest is an integrity binding supplied by the operator, not a signature or proof of publisher identity. Restore requires the exact expected digest and validates the archive schema, paths, types, member set, sizes, modes, and Git blob bytes before creating files. Do not accept archive and digest from unrelated trust sources.

## Safety design

- No shell command strings, remote commands, scheduler, credential service, or implicit repo/root discovery.
- Read-only by default. Apply binds an immutable plan and exact SHA-256, rechecks base and candidate identity, archives before removal, and never uses `--force` or recursive deletion.
- Restore requires an absent path, verifies a trusted archive digest, refuses symlink components and path traversal, writes files exclusively without following symlinks, and retains partial recovery state on failure.
- Unknown state is refused. Submodules, LFS, symlinks, ignored files, sparse/shallow cases, and non-regular archive members are not supported for retirement.

For vulnerability reports, include a minimal reproduction using a disposable repository, affected version/commit, expected versus actual behavior, and a risk assessment. Avoid sharing exploitable archive payloads publicly until coordinated disclosure is complete.
