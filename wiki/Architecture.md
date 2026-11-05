# Architecture

This page explains how cli-router works and why it is built this way.

## The pipeline

A task moves through four stages. Each stage is a plain Python object, and the whole plugin uses only the standard library.

1. **Route picking.** The router reads the quota cache, applies the 20% reserve, and ranks the remaining candidates. Ranking is deterministic by default: a public benchmark score, with cost as tiebreak.
2. **Execution.** The chosen CLI runs under sandbox controls with its own memory limit. Most attempts use bubblewrap; Codex implementation attempts use its native sandbox. The worker edits the repository and commits on the current branch.
3. **Review.** Two distinct models in fresh contexts and separate disposable snapshots review the change independently. Both must pass. A failed review reopens the task with the reviewers' required changes, and the pipeline returns to stage 1.
4. **Record.** Every attempt, verdict, and quota reading lands in a task record under `runtime/cli-tasks/<task_id>.json`. Nothing lives only in memory.

## Design decisions

**Deterministic ranking by default.** The router must work on a fresh install with no extra accounts. Benchmark scores come from a shipped public dataset, so routing needs no service call. The optional Jev plugin upgrades ranking to a live LLM call for users who want it. Its absence is a ranking warning. Work retries still require a decision from the configured OpenRouter Jev model (`typesafe/jev-1.14`); if it is missing or unavailable, the retry blocks.

**Fail open on quota.** A provider that rate limits its usage endpoint must not take the route away. The router keeps the last good entry and stays eligible. This trade favors a slightly stale quota reading over a dead route.

**One worker at a time.** The router runs a single protected CLI worker. Concurrency would multiply sandbox surface and review ambiguity before it multiplies value.

**Fail closed on memory.** Cross attempt memory and the review gate run through ai-memory. When the memory stack is unreachable, the task reports `memory_blocked` instead of continuing blind. A task that cannot remember its failed approaches cannot retry them honestly.

**Sandbox write controls.** The bubblewrap launcher mounts the host root read only, then permits writes to the change repository, selected Git metadata, CLI state, and shared `/tmp`. Hooks, config, and `HEAD` bind read only. Host reads remain possible except for masked paths, and network access is open. Codex implementation attempts use its native sandbox; see [the sandbox contract](../docs/sandbox.md).

## What the router refuses to do

* It never runs a worker outside the sandbox.
* It never counts a change as done without two passing reviews.
* It never sends task data to a service beyond the configured provider CLIs, Jev, and ai-memory.
