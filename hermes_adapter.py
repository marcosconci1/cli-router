"""Compatibility boundary for the Hermes APIs used by this plugin."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

_CONTEXT: Any | None = None


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
