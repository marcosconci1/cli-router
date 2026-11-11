# Installation

This page covers a vanilla Hermes install: how to install the plugin, how to enable it, and how to run the first task.

## Install

```bash
hermes plugins install marcosconci1/cli-router --enable
hermes cli-router setup
```

The install uses only this repository's files. `--enable` adds the plugin to `plugins.enabled`; Hermes turns the `cli_router` toolset on by default for platforms with no saved `platform_toolsets` list. `setup` creates `<hermes-home>/cli-router.yaml` if absent (never overwriting it) and checks the machine. Then restart the gateway (`hermes gateway restart`) and open a fresh session; a running session never picks up plugin changes.

## Advanced: per-profile settings

Optional `plugins.entries` settings in the profile's `config.yaml`:

```yaml
plugins:
  entries:
    cli-router:
      settings:
        # Host-wide protected worker/reviewer concurrency (default 2, range 1-8).
        max-workers: 2
        # Optional for ranking; used for retry-handoff assessment. When Jev is
        # unavailable, retries fall back to deterministic progress-based continuation.
        connection:
          backend: openrouter
          model: typesafe/jev-1.14
        # Turn routes on or off per CLI (absent means enabled).
        routing.providers:
          codex: true
          claude: true
```

Jev credentials and the router's `settings.connection` are separate. The router uses its own connection namespace, and the installed Jev version determines its expected secret name. Follow [Optional integrations](optional-integrations.md#install-and-configure-jev) for version-aware setup.

A profile that keeps an explicit `platform_toolsets` list must include the toolset (`--enable` adds it for you; add it by hand if you edit the list yourself):

```yaml
platform_toolsets:
  discord:
    - cli_router
```

## Per-install cli-router settings

Provider toggles, model exclusions, Jev, ai-memory, review and `max-workers` live in `<hermes-home>/cli-router.yaml`, outside the plugin directory. Reinstalling or updating the plugin does not touch the Hermes home. See [Configuration](configuration.md) for the `CLI_ROUTER_CONFIG` override and legacy fallback.

`quota-reserve` also lives in `cli-router.yaml` (default `30`, range `0` to `99`). A subscription route is excluded when any percentage window has the configured percent or less remaining. The same setting can be supplied through the plugin's Hermes settings.

## Multiple profiles (several bots)

Each profile or bot needs its own plugin install (or a symlink to one local clone), its own `plugins.enabled` entry (`--enable`), its own `platform_toolsets` entries if the profile saves an explicit list, and its own `plugins.entries` settings. Restart the gateway once after updating the profiles; one restart covers them all. The plugin name is `cli-router`, while the toolset name is `cli_router` (underscore).

`max-workers` is a host-wide limit for protected worker and reviewer attempts across repositories and profiles. It defaults to `2` and accepts integers from `1` through `8`; set it in `cli-router.yaml` (see [Configuration](configuration.md)) or, as before, in the Hermes plugin settings. All active cli-router installs on a host must use the same value: a different configured value waits until current workers finish, then becomes the limit for the next active group. Restart gateways after changing it; capacity changes do not interrupt running attempts. The shared host admission lock coordinates cli-router installs that use the same user cache; it does not coordinate other tools or users.

Each repository keeps its existing task lock: shared for analysis and exclusive for changes. A same-repository lock conflict is reported as repository busy; a full host pool is reported as worker capacity reached; insufficient RAM, memory pressure, or missing telemetry is reported as a memory admission block. Workers retain their per-process systemd memory, swap, task-count, OOM, and runtime limits.

## First run

Here `<plugin-dir>` is the directory Hermes reports after install, typically `~/.hermes/profiles/<profile>/plugins/cli-router`.

1. Create the config and check prerequisites from a terminal:

```bash
hermes cli-router setup
```

`hermes cli-router setup --check` does not create or change config; the doctor's writability check may create the runtime directory. It prints local readiness checks. Use `--strict --json` for machine-readable output and strict warning handling. See [setup choices and exit codes](optional-integrations.md#configure-cli-router-choices). The lower-level `python3 <plugin-dir>/doctor.py` runs the raw checks.

Every setup check reports `[ok]`, `[warn]`, `[skip]`, or `[fail]`. Jev ranking failures use deterministic ranking, and failed handoff assessment uses the progress rule. ai-memory `enabled` blocks when preflight is unavailable; `auto` uses one standalone worker attempt; `disabled` always runs standalone. Even with Jev fallback, a managed ai-memory handoff confirmation failure stops the next attempt. Failed bubblewrap namespace probes are blockers. `hermes cli-router setup --check` is the doctor entry point. The `diagnose` action writes a redacted local bundle for bug reports. See [troubleshooting](troubleshooting.md) for stable degraded codes and safe checks.

2. Submit a real task through the `cli_router` tool:

```json
{"action": "submit", "mode": "change", "repository": "/path/to/repo",
 "task": "Add a README badge that reflects the test status",
 "label": "add README test badge",
 "acceptance_criteria": [{"type": "python_compiles", "path": "app.py"}]}
```

The optional label is a short name shown in status and wait; omit secrets and prompts. The submit response returns an `owner_token`.

3. Poll with `{"action": "wait", "task_id": "<id from step 2>", "owner_token": "<token from the submit response>"}`. A healthy run ends `completed`; with the default `independent-review: true` the record holds two independent review verdicts, and with it switched off the record says the review was skipped. The task record under `runtime/cli-tasks/<task_id>.json` holds the full attempt history. Check any nonempty `degraded` array in submit, status, and wait replies. See [Optional integrations](optional-integrations.md#read-degraded-records) for the reporting contract.

While a task runs, ask your agent to poll with `wait`. Each response carries `phase`, `updated_at` and `latest_event`; once routing starts it also carries `route` and `routes_used`, and a task submitted with a `label` also shows it. A healthy run walks through queued, worker running, reviewing, and verifying, then ends `completed` with two independent review verdicts by default. Long stretches in `worker_running` are normal, since workers can run for up to 30 minutes.

Codex summaries use the final assistant message from its JSONL output. Command
output cannot supply a review verdict. For analysis tasks with a path in the
acceptance criteria, Codex starts a fresh session and must complete at least one
shell or MCP tool call. A zero-tool answer fails verification and enters the
normal handoff flow; this guard does not prove every requested file was read.

Optionally pin the worker to one CLI or model by adding `"cli": "claude"` or `"model": "claude-opus-5-5"` to the submit call. The pin narrows the routing to that route and skips automatic ranking; the independent reviews still pick their own models. A pin that matches nothing is refused at submit with the list of installed options.

## Doctor

`setup --check` checks bubblewrap availability and user namespaces, Git and `systemd-run`, installed provider CLIs, the ai-memory CLI and server, the local Jev connection and class, quota cache existence and staleness, and a writable `runtime/cli-tasks/` directory. The Jev check does not prove a live request. The router also keeps a local journal at `runtime/cli-tasks/events.log.jsonl` (task start/finish and doctor snapshots), trimmed to 30 days and 1 MB.

## Development and tests

From the repository root:

```bash
uv sync --locked
uv run --locked python -m pytest tests -q
```

The Hermes contract tests use `~/.hermes/hermes-agent` by default. Set `HERMES_AGENT_DIR` to another source checkout, or they skip when no checkout is present.

## Requirements

* Hermes 0.22 or newer.
* Linux with `bwrap` (bubblewrap), Git, and a systemd user manager (`systemd-run`).
* At least one authenticated provider CLI, for example `codex`, `kimi`, `agy`, `claude`, or `opencode`.
* The experimental OpenRouter free fallback is opt-in: set `providers.opencode.enabled: true` in `cli-router.yaml` to enable it. It is off by default and does not affect subscription routes when the setting is omitted.
* On Ubuntu 24.04 and newer, AppArmor may restrict unprivileged user namespaces, which bubblewrap needs for the sandbox. The common fix is `sudo sysctl kernel.apparmor_restrict_unprivileged_userns=0`; understand the security tradeoff before applying it. To persist it across reboots, place the setting in a file such as `/etc/sysctl.d/99-userns.conf` instead of running sysctl by hand each boot.
* Optional: an authenticated ai-memory CLI and server (ai-memory 3.1.0 or newer) for cross-attempt memory and ledger-confirmed reviews. With `ai-memory: auto` (the default), the router uses it when ready and otherwise runs one standalone worker attempt; `enabled` requires it and `disabled` runs without it.
* [Optional integrations](optional-integrations.md) explains ai-memory installation and auth, Jev's version-specific secret names, the required router connection namespace, and local versus live checks.
* `uv` is needed only for development and tests.
