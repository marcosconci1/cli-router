# Installation

This page covers a vanilla Hermes install: how to install the plugin, how to enable it, and how to run the first task.

## Install

```bash
hermes plugins install marcosconci1/cli-router
```

The install uses only this repository's files. Nothing is imported from sibling modules.

## Enable it

Add the plugin to your Hermes profile's `config.yaml`:

```yaml
plugins:
  enabled:
    - cli-router
  entries:
    cli-router:
      allow_tool_override: true
      settings:
        # Optional for ranking; required for retry handoff after a failed attempt.
        connection:
          backend: openrouter
          model: typesafe/jev-1.14
        # Optional. Turn routes on or off per CLI (absent means enabled).
        routing.providers:
          codex: true
          kimi: true
          agy: true
          claude: true

# List the platforms this profile serves; cli_router must appear under each platform in use.
platform_toolsets:
  discord:
    - cli_router
  cli:
    - cli_router
```

Then restart the gateway and open a fresh session. A running session never picks up plugin changes.

## First run

Here `<plugin-dir>` is the directory Hermes reports after install, typically `~/.hermes/profiles/<profile>/plugins/cli-router`.

1. Check prerequisites from a terminal:

```bash
python3 <plugin-dir>/doctor.py
```

You can also ask Hermes to run the tool action `{"action": "doctor"}`. Every check prints `[ok]`, `[warn]`, or `[fail]` with a hint. `warn` entries describe reduced capability: deterministic ranking can proceed without Jev, but work retry handoff cannot. Missing or unauthenticated ai-memory and failed bubblewrap namespace probes are blockers. `fail` entries must be fixed before tasks will run. The `{"action": "diagnose"}` action writes a redacted bundle (version, Python and platform, doctor check statuses, the last 50 local journal lines, and the 10 most recent task states) to `runtime/cli-tasks/diagnose-<timestamp>.txt` and returns its path and text; it never includes prompts, repository paths, owner tokens, or command lines, and nothing leaves the machine.

2. Submit a real task through the `cli_router` tool:

```json
{"action": "submit", "mode": "change", "repository": "/path/to/repo",
 "task": "Add a README badge that reflects the test status",
 "acceptance_criteria": [{"type": "python_compiles", "path": "app.py"}]}
```

3. Poll with `{"action": "wait", "task_id": "<id from step 2>"}`. A healthy run ends `completed` with two independent review verdicts. The task record under `runtime/cli-tasks/<task_id>.json` holds the full attempt history.

While a task runs, ask your agent to poll with `wait`. Each response carries `phase`, `updated_at`, and `latest_event`, so you can see where the task stands. A healthy run walks through queued, worker running, reviewing, and verifying, then ends `completed` with two independent review verdicts. Long stretches in `worker_running` are normal, since workers can run for up to 30 minutes.

Codex summaries use the final assistant message from its JSONL output. Command
output cannot supply a review verdict. For analysis tasks with a path in the
acceptance criteria, Codex starts a fresh session and must complete at least one
shell or MCP tool call. A zero-tool answer fails verification and enters the
normal handoff flow; this guard does not prove every requested file was read.

Optionally pin the worker to one CLI or model by adding `"cli": "claude"` or `"model": "claude-opus-5-5"` to the submit call. The pin narrows the routing to that route and skips automatic ranking; the independent reviews still pick their own models. A pin that matches nothing is refused at submit with the list of installed options.

## Doctor

`doctor.py` checks: bubblewrap availability and user namespaces, Git and `systemd-run`, installed provider CLIs, the ai-memory CLI and server, the Jev connection (optional for ranking, required for retry handoff), quota cache existence and staleness, and a writable `runtime/cli-tasks/` directory. The router also keeps a local journal at `runtime/cli-tasks/events.log.jsonl` (task start/finish and doctor snapshots), trimmed to 30 days and 1 MB.

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
* On Ubuntu 24.04 and newer, AppArmor may restrict unprivileged user namespaces, which bubblewrap needs for the sandbox. The common fix is `sudo sysctl kernel.apparmor_restrict_unprivileged_userns=0`; understand the security tradeoff before applying it. To persist it across reboots, place the setting in a file such as `/etc/sysctl.d/99-userns.conf` instead of running sysctl by hand each boot.
* An authenticated ai-memory CLI and server (ai-memory 3.1.0 or newer) for cross attempt memory and the review gate.
* Pointers: Jev ships as the hermes-jev Hermes plugin (install it the same way if retry handoff is wanted), and ai-memory needs an authenticated CLI and server per the ai-memory project's install documentation (https://github.com/akitaonrails/ai-memory).
* Jev configured for OpenRouter with model `typesafe/jev-1.14` is optional for ranking and required for retry handoff. Without it, ranking is deterministic, but a failed work attempt that needs another worker blocks.
* `uv` is needed only for development and tests.
