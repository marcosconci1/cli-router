"""Diagnostic health checks for cli-router dependencies and runtime."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from . import ai_memory_bridge, hermes_adapter
except ImportError:
    import ai_memory_bridge
    import hermes_adapter


def _resolve_home(home: str | os.PathLike[str] | None = None) -> Path | None:
    if home is not None:
        return Path(home).resolve()
    try:
        return hermes_adapter.hermes_home(Path(__file__).resolve().parent)
    except Exception:
        env_home = os.environ.get("HERMES_HOME")
        if env_home:
            return Path(env_home).resolve()
    return None


def _check_bubblewrap() -> dict[str, str]:
    name = "bubblewrap"
    bwrap_path = shutil.which("bwrap") or shutil.which("bubblewrap")
    if not bwrap_path:
        return {
            "name": name,
            "status": "fail",
            "hint": "install bubblewrap (e.g. apt-get install bubblewrap or distro package manager)",
        }
    try:
        proc = subprocess.run(
            [bwrap_path, "--ro-bind", "/", "/", "--", "true"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if proc.returncode == 0:
            return {"name": name, "status": "ok", "hint": "bubblewrap operational"}
        return {
            "name": name,
            "status": "fail",
            "hint": "bubblewrap installed but user namespaces restricted by kernel (check /proc/sys/kernel/unprivileged_userns_clone or AppArmor/sysctl)",
        }
    except subprocess.TimeoutExpired:
        return {
            "name": name,
            "status": "fail",
            "hint": "bubblewrap namespace test timed out",
        }
    except Exception as exc:
        return {
            "name": name,
            "status": "fail",
            "hint": f"bubblewrap namespace test failed ({type(exc).__name__})",
        }


def _check_git() -> dict[str, str]:
    if shutil.which("git"):
        return {"name": "git", "status": "ok", "hint": "git installed"}
    return {
        "name": "git",
        "status": "fail",
        "hint": "install git (e.g. apt-get install git)",
    }


def _check_systemd_run() -> dict[str, str]:
    if shutil.which("systemd-run"):
        return {"name": "systemd-run", "status": "ok", "hint": "systemd-run installed"}
    return {
        "name": "systemd-run",
        "status": "fail",
        "hint": "install systemd (systemd-run required for worker resource limits and cgroups)",
    }


def _check_cli(name: str) -> dict[str, str]:
    installed = False
    try:
        from . import task_manager
        installed = bool(task_manager._installed_cli(name) or shutil.which(name))
    except Exception:
        installed = bool(shutil.which(name))

    if installed:
        return {"name": f"cli:{name}", "status": "ok", "hint": f"{name} CLI installed"}
    return {
        "name": f"cli:{name}",
        "status": "warn",
        "hint": f"install {name} CLI and ensure it is in PATH or npm global bin",
    }


def _check_ai_memory() -> dict[str, str]:
    mode = "auto"
    if hermes_adapter.has_context():
        try:
            cfg = hermes_adapter.get_config("cli-router", {})
            if isinstance(cfg, dict):
                configured = cfg.get("ai-memory")
                if configured is None and isinstance(cfg.get("settings"), dict):
                    configured = cfg["settings"].get("ai-memory")
                if isinstance(configured, str) and configured in {"enabled", "disabled", "auto"}:
                    mode = configured
        except Exception:
            pass
    if mode == "disabled":
        return {"name": "ai-memory", "status": "skip",
                "hint": "ai-memory integration disabled by configuration"}
    # Use submit's authenticated version/server probe, not configuration text.
    try:
        ai_memory_bridge.verify_runtime(Path.cwd())
    except ai_memory_bridge.AiMemoryError as exc:
        if mode == "auto":
            return {"name": "ai-memory", "status": "skip",
                    "hint": "ai-memory optional; router will run standalone (managed memory unavailable)"}
        return {"name": "ai-memory", "status": "fail", "hint": str(exc)}
    except Exception as exc:
        if mode == "auto":
            return {"name": "ai-memory", "status": "skip",
                    "hint": "ai-memory optional; router will run standalone (managed memory unavailable)"}
        return {"name": "ai-memory", "status": "fail",
                "hint": f"ai-memory runtime check failed ({type(exc).__name__})"}
    return {"name": "ai-memory", "status": "ok",
            "hint": "supported ai-memory client and authenticated server reachable"}


def _check_jev_connection() -> dict[str, str]:
    if not hermes_adapter.has_context():
        return {
            "name": "jev-connection",
            "status": "skip",
            "hint": "standalone execution; Jev context check skipped",
        }
    try:
        cfg = hermes_adapter.get_config("cli-router", {})
        if not isinstance(cfg, dict):
            cfg = {}
        connection = cfg.get("settings", {}).get("connection") or cfg.get("connection")
        if not (connection and isinstance(connection, dict) and connection.get("backend") and connection.get("model")):
            return {
                "name": "jev-connection",
                "status": "warn",
                "hint": "deterministic fallback ranking in use; configure settings.connection for work retry handoff",
            }
    except Exception as exc:
        return {
            "name": "jev-connection",
            "status": "warn",
            "hint": f"failed to read Jev configuration: {exc}",
        }
    try:
        hermes_adapter._jev_service_cls()
    except Exception:
        return {
            "name": "jev-connection",
            "status": "warn",
            "hint": "Jev plugin is not loaded in-process; deterministic fallback ranking in use; work retry handoff unavailable",
        }
    return {
        "name": "jev-connection",
        "status": "ok",
        "hint": f"Jev ranking active for {connection.get('backend')}/{connection.get('model')}",
    }


def _check_plugin_wiring(home: str | os.PathLike[str] | None = None) -> dict[str, str]:
    name = "plugin-wiring"
    resolved_home = _resolve_home(home)
    if resolved_home is None:
        return {
            "name": name,
            "status": "fail",
            "hint": "active profile's config.yaml could not be located; run doctor through Hermes or set HERMES_HOME",
        }

    config_path = resolved_home / "config.yaml"
    if not config_path.is_file():
        return {
            "name": name,
            "status": "fail",
            "hint": "active profile's config.yaml could not be located; run doctor through Hermes or set HERMES_HOME",
        }

    try:
        lines = config_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return {
            "name": name,
            "status": "fail",
            "hint": "active profile's config.yaml could not be located; run doctor through Hermes or set HERMES_HOME",
        }

    def without_comment(line: str) -> str:
        return line.split("#", 1)[0].rstrip()

    plugin_enabled = False
    in_plugins = False
    in_enabled = False
    in_toolsets = False
    toolset_found = False
    for raw_line in lines:
        line = without_comment(raw_line)
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        content = line.strip()

        if indent == 0:
            in_plugins = content == "plugins:"
            in_enabled = False
            in_toolsets = content == "platform_toolsets:"
            continue

        if in_plugins:
            if indent == 2 and content == "enabled:":
                in_enabled = True
                continue
            if indent == 2:
                in_enabled = False
            if in_enabled and indent >= 4 and content.startswith("-"):
                entry = content[1:].strip()
                if entry == "cli-router":
                    plugin_enabled = True

        if in_toolsets and re.search(r"(?<![A-Za-z0-9_-])cli_router(?![A-Za-z0-9_-])", content):
            toolset_found = True

    if not plugin_enabled:
        return {
            "name": name,
            "status": "fail",
            "hint": "cli-router is not in plugins.enabled; add it to the active profile's config.yaml",
        }
    if not toolset_found:
        return {
            "name": name,
            "status": "fail",
            "hint": "plugin name is cli-router but the TOOLSET name is cli_router (underscore); add cli_router under each platform you use in platform_toolsets - a missing toolset entry is silent: the plugin loads but its tools never reach the model",
        }
    return {"name": name, "status": "ok", "hint": "plugin enabled and cli_router toolset configured"}


def _check_quota_cache(home: Path | None) -> dict[str, str]:
    name = "quota-cache"
    if home is None:
        return {
            "name": name,
            "status": "warn",
            "hint": "hermes home path unknown; cannot locate quota_cache.json",
        }
    cache_path = home / "quota_cache.json"
    if not cache_path.is_file():
        return {
            "name": name,
            "status": "warn",
            "hint": f"quota cache not found at {cache_path}; will refresh or use manual limits",
        }
    try:
        # Check cache age
        import json
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        fetched_at = payload.get("fetched_at")
        age_seconds: float | None = None
        if fetched_at:
            stamp = datetime.fromisoformat(fetched_at)
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            age_seconds = (datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds()
        else:
            mtime = cache_path.stat().st_mtime
            age_seconds = datetime.now().timestamp() - mtime

        if age_seconds is not None:
            age_hours = age_seconds / 3600.0
            if age_seconds > 3600:
                return {
                    "name": name,
                    "status": "warn",
                    "hint": f"quota cache exists but is older than 1h ({age_hours:.1f}h old); refresh recommended",
                }
            return {
                "name": name,
                "status": "ok",
                "hint": f"quota cache exists and is fresh ({age_seconds:.0f}s old)",
            }
        return {
            "name": name,
            "status": "ok",
            "hint": "quota cache exists",
        }
    except Exception as exc:
        return {
            "name": name,
            "status": "warn",
            "hint": f"error reading quota cache at {cache_path}: {exc}",
        }


def _check_runtime_dir(home: Path | None) -> dict[str, str]:
    name = "runtime-dir"
    if home is None:
        return {
            "name": name,
            "status": "fail",
            "hint": "hermes home path cannot be resolved; set HERMES_HOME",
        }
    target = home / "runtime" / "cli-tasks"
    try:
        target.mkdir(parents=True, exist_ok=True)
        # Test writability
        with tempfile.NamedTemporaryFile(dir=str(target), prefix=".doctor-probe-", delete=True) as probe:
            probe.write(b"ok")
            probe.flush()
        return {
            "name": name,
            "status": "ok",
            "hint": f"runtime directory writable ({target})",
        }
    except Exception as exc:
        return {
            "name": name,
            "status": "fail",
            "hint": f"runtime directory {target} is not writable: {exc}",
        }


def run_checks(home: str | os.PathLike[str] | None = None) -> list[dict[str, str]]:
    resolved_home = _resolve_home(home)
    checks: list[dict[str, str]] = []
    checks.append(_check_bubblewrap())
    checks.append(_check_git())
    checks.append(_check_systemd_run())
    for cli_name in ("claude", "codex", "agy", "kimi"):
        checks.append(_check_cli(cli_name))
    checks.append(_check_ai_memory())
    checks.append(_check_jev_connection())
    checks.append(_check_plugin_wiring(resolved_home))
    checks.append(_check_quota_cache(resolved_home))
    checks.append(_check_runtime_dir(resolved_home))
    return checks


def main() -> int:
    checks = run_checks()
    has_fail = False
    for check in checks:
        status = check["status"]
        if status == "fail":
            has_fail = True
        prefix = f"[{status}]"
        print(f"{prefix:<7} {check['name']}: {check['hint']}")
    return 1 if has_fail else 0


if __name__ == "__main__":
    sys.exit(main())
