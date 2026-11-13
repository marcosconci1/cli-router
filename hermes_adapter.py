"""Compatibility boundary for the Hermes APIs used by this plugin."""
from __future__ import annotations

import os
import sys
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

_CONTEXT: Any | None = None
_PROGRESS_MESSAGES: OrderedDict[str, Any] = OrderedDict()
_PROGRESS_LOCK = threading.Lock()
_MAX_PROGRESS_MESSAGES = 8


def register(ctx: Any, schema: dict[str, Any], handler: Any) -> None:
    global _CONTEXT
    _CONTEXT = ctx
    ctx.register_tool(
        name="cli_router",
        toolset="cli_router",
        schema=schema,
        handler=handler,
        description=schema["description"],
        emoji="🔀",
    )


def has_context() -> bool:
    return _CONTEXT is not None


def clear_progress(task_id: str) -> None:
    with _PROGRESS_LOCK:
        _PROGRESS_MESSAGES.pop(task_id, None)


def post_and_edit(text: str, task_id: str | None = None, *, progress: bool = False,
                  message_id: str | None = None) -> Any | None:
    """Post or reuse a progress handle without allowing Hermes failures to affect dispatch."""
    handle = None
    if progress and task_id:
        with _PROGRESS_LOCK:
            handle = _PROGRESS_MESSAGES.get(task_id)
            if handle is not None:
                _PROGRESS_MESSAGES.move_to_end(task_id)
    if handle is None and message_id:
        # Hermes cannot reconstruct a conversation-bound PluginMessage from only its message id.
        return None
    if handle is None and _CONTEXT is None:
        return None
    try:
        if handle is not None:
            handle.edit(text)
            return handle
        result = _CONTEXT.post_message(text)
        if result is None or result is False or getattr(result, "refused", False):
            return None
        if progress and task_id and getattr(result, "message_id", None):
            with _PROGRESS_LOCK:
                _PROGRESS_MESSAGES[task_id] = result
                _PROGRESS_MESSAGES.move_to_end(task_id)
                while len(_PROGRESS_MESSAGES) > _MAX_PROGRESS_MESSAGES:
                    _PROGRESS_MESSAGES.popitem(last=False)
        return result
    except Exception as exc:
        try:
            try:
                from . import task_manager
            except ImportError:  # plain-module layout (tests, CLI probes)
                import task_manager
            task_manager._record_route_warning(task_id, "notification", exc)
        except Exception:
            pass
        return None


def get_config(name: str, default: Any = None) -> Any:
    if _CONTEXT is None:
        raise RuntimeError("Jev plugin context is unavailable")
    return _CONTEXT.get_config(name, default)


def jev_service() -> Any:
    if _CONTEXT is None:
        raise RuntimeError("Jev plugin context is unavailable")
    return _jev_service_cls()(_CONTEXT)


def _jev_service_cls() -> Any:
    for name, module in tuple(sys.modules.items()):
        if name.endswith("hermes_jev.service") or (
                name.endswith(".service") and hasattr(module, "Service") and hasattr(module, "MODELS")):
            service_cls = getattr(module, "Service", None)
            if service_cls:
                return service_cls
    raise RuntimeError("Jev plugin is not loaded")


def hermes_home(plugin_dir: Path) -> Path:
    profile_home = plugin_dir.parent.parent
    if plugin_dir.parent.name == "plugins" and profile_home.parent.name == "profiles":
        try:
            from hermes_constants import get_default_hermes_root
        except ImportError:
            pass
        else:
            return Path(get_default_hermes_root()).resolve() / "profiles" / profile_home.name
    try:
        from hermes_constants import get_hermes_home
    except ImportError:
        home = os.environ.get("HERMES_HOME")
        if home:
            return Path(home).resolve()
        raise
    return Path(get_hermes_home()).resolve()
