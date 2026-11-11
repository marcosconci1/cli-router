# Changelog

## 0.7.0

- submit accepts an optional short label, shown in status and wait.
- status and wait now show which CLI and model run the task (`route`, `routes_used`)
- hermes cli-router setup: one-command first run (creates cli-router.yaml, checks the machine); install is now two commands.
- doctor no longer reports a fresh install as failing plugin-wiring when no platform_toolsets list exists.

## 0.6.0

- The OpenRouter free fallback is now opt-in (providers.opencode.enabled: true), so default installs no longer fetch the catalog or warn on every task.
- Add a `cli-router.yaml` config file in the Hermes home for providers, model exclusions, Jev, ai-memory, review and worker limits.
- Independent review can be switched off, and a skipped review is recorded as skipped, never as passed.
- A bounded worker pool replaces the single host-wide slot, so different repositories run side by side; the default is 2 workers and same-repository changes stay exclusive.
- Configuration errors fail loudly.
- Task handoff waits up to 90 seconds for the ai-memory ledger before stopping, so a slow write no longer blocks a healthy task.
- Align the `plugin.yaml` version with `pyproject.toml`.

## 0.5.0

- Add a local diagnostic journal (`runtime/cli-tasks/events.log.jsonl`, 30 days / 1 MB retention) and a `diagnose` action that writes a redacted bundle for bug reports. No telemetry and no network calls.
- Route creative tasks using Design Arena ELO evidence in the benchmark index and profiles, with a per-model routing toggle.
- Make ai-memory integration optional with `enabled`, `disabled`, and `auto` modes; standalone mode runs workers and reviewers without managed memory.

## 0.4.1

- Add a tokenless `recover` action: reaps dead tasks of a repository to `interrupted` with a checkpoint capture, guarded by the repository lock, so a crashed orchestrator no longer needs the owner token to clean up.
- Make the Jev handoff gate resilient: the assessment retries once and falls back to a deterministic continue-on-progress decision recorded as a route warning, instead of blocking tasks whose work is already done.
- Let analysis-mode tasks share the repository lock under a shared non-blocking lock while change tasks keep exclusivity; conflicting submissions get an immediate busy error and analysis submits never reap orphaned tasks.
- Check the repair-attempt marker against every fetched ledger event before rendering the bounded context, so byte-limit truncation can no longer hide the marker and block the repair leg.

## 0.4.0

- Initial public release. Routes coding tasks across Codex, Claude Code, Kimi and Antigravity with quota-aware rotation, sandboxed execution and independent review.
