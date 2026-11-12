# The sandbox

Workers execute locally under sandbox controls. This page explains what the sandbox contains and what a worker can and cannot do.

## What the sandbox does

* The bubblewrap (`bwrap`) launcher mounts the host root read only, then adds writable mounts for a change task's repository, selected Git metadata, CLI state, and shared `/tmp`. Analysis mounts the repository read only. Workers receive memory limits through `systemd-run`.
* Host files are readable except for paths explicitly masked by the launcher. Secret masking covers matching files under the user's home directory outside the target repository; it is not a guarantee that all host secrets are hidden. Repository secrets remain accessible. The scan skips `.git`, `.venv`, `venv`, `node_modules`, `.cache`, and `__pycache__`, and does not follow directory symlinks. Nested `.env`, `.env.*`, and `credentials*` files elsewhere under the home directory are masked.
* AGY always runs standalone, without managed ai-memory (a workaround for ai-memory not recording AGY prompts). The launcher overlays AGY's MCP, hook, and MCP OAuth token files with copies stripped of ai-memory entries; unreadable or malformed files become empty objects, and symlinked config paths make the launch fail. This filter is a best-effort routing policy, not a security boundary: AGY can still read the host's ai-memory configuration through the read-only host mount, and a concurrent change to those paths between inspection and launch can let AGY load an unfiltered file.
* In bubblewrap attempts, network access is open. The sandbox does not prevent a worker from transmitting readable data.
* Codex implementation attempts use Codex's native sandbox rather than this outer bubblewrap launcher; the launcher's secret masks do not apply to those attempts. Codex review attempts use the outer sandbox.

## What a worker can do

* Edit files in the target repository in change mode.
* Read host files that are not masked, access the network, and write to the permitted CLI state and temporary paths.
* Commit its own result on the checkout's current branch.

## What a worker cannot do

* Plant Git hooks that would run outside the sandbox.
* Switch or detach the checkout of a branch shared with other sessions.
* Escape through Git configuration.

## How Git stays safe

The sandbox binds the task's Git metadata through Git itself:

* A task below its work tree root, or a linked worktree, gets its common Git directory bound writable.
* Hooks, including a `core.hooksPath` inside the work tree, stay read only.
* Git `config` and `HEAD` stay read only.

These controls permit commits while protecting the checkout's hooks and branch pointer. CLI state and temporary files can persist outside the repository.

## The review gate

A commit alone does not finish a task. Two distinct models in fresh contexts and separate disposable snapshots review the change independently by default. Both must pass. The review can be switched off in `cli-router.yaml`; see [Configuration](configuration.md). Reviewers may share a provider, and the implementer's model remains eligible in a fresh context. A failed review sends the task back with the reviewers' required changes, and the router picks a route again.
