"""cli router entry point with delegated verification."""
from __future__ import annotations

import json
from typing import Any

TASK_SCHEMA = {
    "name": "cli_router",
    "description": "Route a coding task to a CLI (Codex, Claude Code, Kimi and Antigravity), then wait for, inspect, or cancel it. After submit, use wait repeatedly in the same turn until terminal state; never use terminal sleep timers or repository polling. Every task includes independent external verification and at most one repair cycle. Submit requires at least one structured acceptance criterion.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["submit", "wait", "status", "cancel", "doctor", "diagnose"]},
            "repository": {"type": "string", "description": "Existing local Git repository path; required for submit."},
            "task": {"type": "string", "description": "Task for the CLI worker; required for submit."},
            "mode": {"type": "string", "enum": ["analysis", "change"]},
            "label": {"type": "string", "description": "Optional short name for the task, up to 60 characters, shown in status and wait. Do not put secrets or prompts in it."},
            "cli": {"type": "string", "description": "Optional pin for submit: restrict routing to one CLI (e.g. claude, codex, kimi, agy, opencode). Reviews still run independently."},
            "model": {"type": "string", "description": "Optional pin for submit: restrict routing to one model (e.g. claude-opus-5-5). Reviews still run independently."},
            "acceptance_criteria": {"type": "array", "minItems": 1, "maxItems": 12, "items": {
                "type": "object", "properties": {
                    "type": {"type": "string", "enum": ["file_exists", "file_changed", "python_compiles", "git_diff_clean"]},
                    "path": {"type": "string", "maxLength": 300}}, "required": ["type"], "additionalProperties": False}},
            "task_id": {"type": "string"},
            "owner_token": {"type": "string", "description": "Returned only by submit; required for wait, status, and cancel."},
            "timeout_seconds": {"type": "number", "minimum": 0, "maximum": 300, "description": "For wait, maximum seconds to block before returning compact status. Default 300."},
            "include_history": {"type": "boolean", "description": "For status or wait, include full attempts, memory details, and events. Default returns a compact view."},
        },
        "required": ["action"], "additionalProperties": False,
    },
}


def _cli_router(args: dict[str, Any], **_: Any) -> str:
    from .task_manager import dispatch
    return json.dumps(dispatch(args), ensure_ascii=False)


def register(ctx) -> None:
    from . import hermes_adapter
    hermes_adapter.register(ctx, TASK_SCHEMA, _cli_router)
    from pathlib import Path
    ctx.register_skill("cli-code-router", Path(__file__).parent / "skills" / "cli-code-router" / "SKILL.md")
    if hasattr(ctx, "register_cli_command"):
        from . import setup_command
        ctx.register_cli_command(
            name="cli-router", help="Set up and check cli-router",
            setup_fn=setup_command.setup_parser, handler_fn=setup_command.handle,
            description="Create cli-router.yaml if absent and check this machine.")
