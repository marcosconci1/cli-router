# Troubleshooting

When you choose ai-memory or Jev and it fails, cli-router does not hide it. It adds a
`degraded` record to the task. Each record has a stable `code`, a `title`, an `effect`
(what you lose), a `detail` (why it failed this time), a `fix`, and a `docs` link to the
section below. Anything you did not choose stays silent.

Find a record in these places:

- The submit reply, when the problem is found while routing.
- The `status` and `wait` views, under `degraded`. The key is absent when nothing is wrong.
- `doctor`: the ai-memory and Jev checks show `code` and `chosen`.
- The journal `runtime/cli-tasks/events.log.jsonl`: one `degraded` event per code per task.
- The `diagnose` bundle: it lists the codes found in the recent journal.

A component counts as chosen when `ai-memory` is `enabled` or `auto` in `cli-router.yaml`
or the direct ai-memory plugin setting, or when `jev` is `true` or cli-router's own
`settings.connection` is set.

## AIM_CLI_MISSING

**Symptom.** The task ran standalone and the record says the ai-memory CLI was not found.

**Cause.** The `ai-memory` command is not installed or not on PATH for the router process.

**Fix.** Install the ai-memory CLI and make sure `ai-memory` is on PATH.

**How to verify.** Run `ai-memory --version` in the same environment as Hermes.

## AIM_AUTH_MISSING

**Symptom.** The record says authentication is unavailable.

**Cause.** `AI_MEMORY_AUTH_TOKEN` is not set in the router process.

**Fix.** Set AI_MEMORY_AUTH_TOKEN in the environment of the router process.

**How to verify.** Run `ai-memory status` and confirm it exits successfully, then run `hermes cli-router setup --check` and confirm the ai-memory check is `ok`.

## AIM_VERSION_UNSUPPORTED

**Symptom.** The record says a newer ai-memory is required.

**Cause.** The installed ai-memory is older than 3.1.0.

**Fix.** Upgrade the ai-memory CLI to version 3.1.0 or newer.

**How to verify.** Run `ai-memory --version`. It must print 3.1.0 or higher.

## AIM_SERVER_UNAVAILABLE

**Symptom.** The record says the server is unavailable.

**Cause.** The ai-memory server is down, or it rejected the token.

**Fix.** Start the ai-memory server and check AI_MEMORY_AUTH_TOKEN with `ai-memory status`.

**How to verify.** Run `ai-memory status` and confirm it exits successfully, then run `hermes cli-router setup --check` and confirm the ai-memory check is `ok`.

## AIM_CHECK_FAILED

**Symptom.** The record shows another runtime check failure.

**Cause.** The version check or a request to ai-memory failed, for example a timeout.

**Fix.** Run `ai-memory --version` and `ai-memory status` in the repository and fix the error they show.

**How to verify.** Run `hermes cli-router setup --check`. The ai-memory check should be `ok`.

## AIM_ATTEMPT_UNCONFIRMED

**Symptom.** An attempt or review lacks ledger confirmation; automatic handoff cannot continue and an affected review is incomplete.

**Cause.** ai-memory did not confirm the attempt. The detail is the last line of the worker error output, for example a sandbox mount error.

**Fix.** Read the detail, fix that cause, then run `ai-memory status` and submit the task again.

**How to verify.** Submit again. The task should pass memory confirmation and the record should not return.

## JEV_PLUGIN_NOT_LOADED

**Symptom.** Routing used the deterministic ranking.

**Cause.** The Jev plugin is not loaded in the Hermes process.

**Fix.** Enable the Jev plugin in Hermes with `hermes plugins enable jev` and restart Hermes.

**How to verify.** Run `hermes cli-router setup --check`. The jev-connection check should be `ok`.

## JEV_NOT_CONFIGURED

**Symptom.** Routing used the deterministic ranking.

**Cause.** cli-router's own plugin settings have no `settings.connection`, or it has no backend or model. Jev is constructed with cli-router's Hermes context.

**Fix.** Set `settings.connection` (backend and model) in cli-router's own plugin settings.

**How to verify.** Run `hermes cli-router setup --check`. The jev-connection check should be `ok`.

## JEV_MODEL_UNSUPPORTED

**Symptom.** Routing used the deterministic ranking.

**Cause.** The connection is not backend `openrouter` with model `typesafe/jev-1.14`.

**Fix.** Set cli-router's own `settings.connection` to backend openrouter and model typesafe/jev-1.14.

**How to verify.** Submit a task. The record should not return.

## JEV_REQUEST_FAILED

**Symptom.** Routing or handoff fell back after Jev failed twice.

**Cause.** Jev returned an error or a not-ok result, after the single retry.

**Fix.** Check the OpenRouter credential and network, then retry the task.

**How to verify.** Submit a task. The record should not return.
