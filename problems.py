"""Structured, machine-readable records for failures of components the user chose to use.

Modeled on RFC 9457 problem details: a stable code, a constant title, a per-occurrence
detail and a documentation link. Stdlib only.
"""
from __future__ import annotations

import re
from typing import Any

_AIM_EFFECT = ("ai-memory is not used: the task runs standalone with one attempt, no handoff memory, "
               "and reviews are not ledger-confirmed.")
_JEV_EFFECT = "Jev is not used: routes are ranked deterministically and the retry handoff uses the progress rule."

CODES: dict[str, dict[str, str]] = {
    "AIM_CLI_MISSING": {
        "component": "ai-memory", "title": "ai-memory CLI not found", "effect": _AIM_EFFECT,
        "fix": "Install the ai-memory CLI and make sure `ai-memory` is on PATH."},
    "AIM_AUTH_MISSING": {
        "component": "ai-memory", "title": "ai-memory authentication missing", "effect": _AIM_EFFECT,
        "fix": "Set AI_MEMORY_AUTH_TOKEN in the environment of the router process."},
    "AIM_VERSION_UNSUPPORTED": {
        "component": "ai-memory", "title": "ai-memory version unsupported", "effect": _AIM_EFFECT,
        "fix": "Upgrade the ai-memory CLI to version 3.1.0 or newer."},
    "AIM_SERVER_UNAVAILABLE": {
        "component": "ai-memory", "title": "ai-memory server unreachable or token rejected",
        "effect": _AIM_EFFECT,
        "fix": "Start the ai-memory server and check AI_MEMORY_AUTH_TOKEN with `ai-memory status`."},
    "AIM_CHECK_FAILED": {
        "component": "ai-memory", "title": "ai-memory runtime check failed", "effect": _AIM_EFFECT,
        "fix": "Run `ai-memory --version` and `ai-memory status` in the repository and fix the error they show."},
    "AIM_ATTEMPT_UNCONFIRMED": {
        "component": "ai-memory", "title": "ai-memory did not confirm an attempt",
        "effect": ("The task stopped after this attempt: there is no handoff memory, "
                   "no failover, and the review is not ledger-confirmed."),
        "fix": "Read the detail, fix that cause, then run `ai-memory status` and submit the task again."},
    "JEV_PLUGIN_NOT_LOADED": {
        "component": "jev", "title": "Jev plugin not loaded", "effect": _JEV_EFFECT,
        "fix": "Enable the Jev plugin in Hermes with `hermes plugins enable jev` and restart Hermes."},
    "JEV_NOT_CONFIGURED": {
        "component": "jev", "title": "Jev connection not configured", "effect": _JEV_EFFECT,
        "fix": "Set settings.connection (backend and model) in the Jev plugin settings."},
    "JEV_MODEL_UNSUPPORTED": {
        "component": "jev", "title": "Jev connection uses an unsupported model", "effect": _JEV_EFFECT,
        "fix": "Set settings.connection to backend openrouter and model typesafe/jev-1.14."},
    "JEV_REQUEST_FAILED": {
        "component": "jev", "title": "Jev request failed", "effect": _JEV_EFFECT,
        "fix": "Check the OpenRouter credential and network, then retry the task."},
}

_PATH = re.compile(r"(?<![\w.:])/(?:[\w.@+~-]+/)*[\w.@+~-]*")


def _safe_text(value: Any, limit: int = 300) -> str:
    # Reuse task_manager's secret masking; imported lazily because task_manager imports this module.
    try:
        from . import task_manager
    except ImportError:
        import task_manager
    return _PATH.sub("[path]", task_manager._safe_text(value, 2000))[:limit]


def classify_ai_memory_error(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    return code if code in CODES else "AIM_CHECK_FAILED"


def make(code: str, detail: Any) -> dict[str, Any]:
    spec = CODES[code]
    return {"code": code, "component": spec["component"], "chosen": True, "title": spec["title"],
            "effect": spec["effect"], "detail": _safe_text(detail, 300), "fix": spec["fix"],
            "docs": "docs/troubleshooting.md#" + code.lower()}


def add(task_data: dict[str, Any], entry: dict[str, Any]) -> bool:
    """Append an entry once per code; return True when it was new."""
    degraded = task_data.get("degraded")
    if not isinstance(degraded, list):
        degraded = []
    task_data["degraded"] = degraded
    if any(isinstance(item, dict) and item.get("code") == entry["code"] for item in degraded):
        return False
    degraded.append(entry)
    return True


def _plugin_settings() -> dict[str, Any]:
    try:
        from . import hermes_adapter
    except ImportError:
        import hermes_adapter
    if not hermes_adapter.has_context():
        return {}
    try:
        cfg = hermes_adapter.get_config("cli-router", {})
    except Exception:
        return {}
    if not isinstance(cfg, dict):
        return {}
    merged = dict(cfg.get("settings")) if isinstance(cfg.get("settings"), dict) else {}
    merged.update({k: v for k, v in cfg.items() if k != "settings"})
    return merged


def is_chosen(component: str) -> bool:
    """True when the user explicitly opted in to the component (never for defaults)."""
    try:
        from . import hermes_adapter, task_manager
    except ImportError:
        import hermes_adapter
        import task_manager
    try:
        modular = task_manager._load_modular_config()
    except Exception:
        return False
    if component == "ai-memory":
        value = modular["ai-memory"] if "ai-memory" in modular else _plugin_settings().get("ai-memory")
        return value in ("enabled", "auto")
    if component == "jev":
        setting = modular["jev"] if "jev" in modular else _plugin_settings().get("jev")
        if setting is False:
            return False
        if setting is True:
            return True
        if not hermes_adapter.has_context():
            return False
        try:
            missing = object()
            connection = hermes_adapter.get_config("connection", missing)
        except Exception:
            return False
        return connection is not missing and connection is not None
    return False
