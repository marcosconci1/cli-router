# Security policy

## Supported versions

Only the latest release receives security fixes. Update to the newest version before reporting a problem.

## Reporting a vulnerability

Please do not open a public issue for a security report. Use GitHub's private vulnerability reporting: open the repository, choose **Security**, then **Report a vulnerability**. You can also reach the maintainer through a GitHub profile contact.

## What matters most

* A bypass of explicit secret masks or repository write controls.
* A way for a worker to plant a Git hook that runs outside the sandbox.
* Any path that exposes credentials from one project's environment to another.

The sandbox promises in [docs/sandbox.md](docs/sandbox.md) are the security contract. A report that breaks one of those promises is a security issue.

## Current limits and future hardening

The bubblewrap launcher exposes the host root read only, masks selected paths,
and leaves network access open. CLI state and shared `/tmp` are writable. Codex
implementation attempts use its native sandbox and do not receive the launcher's
secret masks. Reading an unmasked host file is a known limitation of the current
contract, not evidence of a sandbox escape.

Future hardening: replace the host-root bind with an explicit allowlist of the
repository, runtime dependencies, and required CLI state. This needs compatibility
testing across providers, Git worktrees, hooks, and credential refresh before it
can become the default. Broader secret masks reduce exposure but do not provide
that read boundary.

## What is not in scope

* A worker damaging the repository it was legitimately given.
* Quota accuracy problems.
* Feature requests.
