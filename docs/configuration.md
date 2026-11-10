# Configuration

cli-router reads an optional per-install file, `cli-router.yaml`, that travels with the plugin installation instead of the Hermes profile.

## Set it up

1. Copy the example next to `task_manager.py` in the plugin directory (typically `~/.hermes/profiles/<profile>/plugins/cli-router`):

   ```sh
   cp <plugin-dir>/cli-router.example.yaml <plugin-dir>/cli-router.yaml
   ```

2. Edit the copy. Keep only the settings you want to change; omitted settings keep their Hermes value or default.
3. Restart the gateway. A running gateway does not pick up edits.

The file is gitignored in this repository, and an update keeps your `cli-router.yaml`.

## Keys

All keys live under a single top-level `cli-router` mapping.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `providers.<cli>.enabled` | bool | `true` | Turns routing to that CLI on or off. CLIs: `codex`, `claude`, `kimi`, `agy`, `opencode`. |
| `providers.<cli>.excluded_models` | list of model IDs | empty | Exact model IDs excluded for that CLI only. Applies to routing, reviews and pins. Empty or absent excludes nothing. |
| `jev` | bool | `true` | `false` uses deterministic ranking and progress-based handoff, with no Jev calls. |
| `ai-memory` | `enabled`, `disabled` or `auto` | `auto` | `enabled` requires managed ai-memory, `disabled` runs standalone, `auto` uses it when available. |
| `independent-review` | bool | `true` | `false` skips only the model reviews. Acceptance checks still run. |
| `max-workers` | integer 1 to 8 | `2` | Host-wide limit on concurrent protected worker and reviewer attempts. |

## Precedence

1. `cli-router.yaml`
2. Hermes plugin settings (the legacy `routing.providers`, `routing.models`, `ai-memory` and `max-workers` keep working)
3. Built-in defaults

Only values the file sets explicitly override the later layers.

## Examples

Bench one CLI:

```yaml
cli-router:
  providers:
    kimi:
      enabled: false
```

Exclude one model from one CLI:

```yaml
cli-router:
  providers:
    codex:
      excluded_models: [model-id]
```

Run without Jev:

```yaml
cli-router:
  jev: false
```

Run without ai-memory:

```yaml
cli-router:
  ai-memory: disabled
```

Switch the model review off:

```yaml
cli-router:
  independent-review: false
```

With review off, the local acceptance checks still run. The task record shows the independent review as skipped ("independent review skipped by cli-router configuration") and never as passed.

## Check it

`python3 <plugin-dir>/doctor.py` includes a `config` check. It reports `ok` with a summary of the active settings (or "no cli-router.yaml (defaults)"), and `fail` with the reason when the file is invalid. An invalid file raises a `ConfigurationError` at load time rather than being ignored. Unknown keys and malformed values are rejected.

Only a YAML subset is supported: indented mappings, booleans, integers, quoted strings and simple lists. Anchors and block scalars are not supported.

## Prerequisites

* A cli-router install; see [Installation](install.md).
* Gateway restart after every edit.
* `max-workers` must match across all active cli-router installs on the host; see [Installation](install.md#multiple-profiles-several-bots).
