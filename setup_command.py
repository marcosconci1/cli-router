"""One-command first run: `hermes cli-router setup [--check]`."""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Callable

try:
    from . import doctor
except ImportError:
    import doctor

_ROOT = Path(__file__).resolve().parent
_TOOLSET_RE = re.compile(r"(?<![A-Za-z0-9_-])cli_router(?![A-Za-z0-9_-])")


def _version() -> str:
    try:
        for line in (_ROOT / "plugin.yaml").read_text(encoding="utf-8").splitlines():
            if line.startswith("version:"):
                return line.split(":", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return "unknown"


def _line(tag: str, text: str) -> str:
    return f"{'[' + tag + ']':<7} {text}"


def _toolset_platforms(home: Path | None) -> list[str] | None:
    """Platforms whose saved platform_toolsets list has cli_router; None when no list is saved."""
    if home is None:
        return None
    try:
        lines = (home / "config.yaml").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    platforms: list[str] = []
    saved = False
    in_block = False
    current = None
    for raw in lines:
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        content = line.strip()
        if indent == 0:
            in_block = content == "platform_toolsets:"
            continue
        if not in_block:
            continue
        saved = True
        if indent == 2 and ":" in content:
            current = content.split(":", 1)[0].strip()
        if current and _TOOLSET_RE.search(content) and current not in platforms:
            platforms.append(current)
    return platforms if saved else None


def _ensure_config(home: Path | None, check_only: bool) -> tuple[str, str]:
    module = doctor._task_manager_module()
    try:
        existing = module._config_path()
    except Exception as exc:
        return "fail", f"invalid CLI_ROUTER_CONFIG: {exc}"
    candidates = [home / "cli-router.yaml"] if home is not None else []
    if existing is not None:
        candidates.insert(0, existing)
    if any(p.is_file() for p in candidates):
        return "ok", "cli-router.yaml present (kept as is)"
    if home is None:
        return "fail", "hermes home could not be located; set HERMES_HOME"
    target = home / "cli-router.yaml"
    if check_only:
        return "skip", "cli-router.yaml not created (--check)"
    try:
        home.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_ROOT / "cli-router.example.yaml", target)
    except OSError as exc:
        return "fail", f"could not create {target}: {exc}"
    return "new", f"created {target} (defaults; edit to taste)"


def run_setup(home: Any = None, check_only: bool = False, out: Callable[[str], Any] = print) -> int:
    resolved = doctor._resolve_home(home)
    out(f"cli-router {_version()} · profile: {resolved.name if resolved else 'unknown'}")
    failed = False

    wiring = doctor._check_plugin_wiring(resolved)
    if wiring["status"] == "ok":
        out(_line("ok", "plugin enabled"))
    else:
        out(_line(wiring["status"], f"plugin wiring: {wiring['hint']}"))
        failed |= wiring["status"] == "fail"

    platforms = _toolset_platforms(resolved)
    if platforms is None:
        out(_line("ok", "tool reachable on: all platforms (Hermes default)"))
    elif platforms:
        out(_line("ok", f"tool reachable on: {', '.join(platforms)}"))
    else:
        out(_line("warn", "tool reachable on: no platform; add cli_router to platform_toolsets"))

    tag, text = _ensure_config(resolved, check_only)
    out(_line(tag, text))
    failed |= tag == "fail"

    checks = [c for c in doctor.run_checks(resolved) if c["name"] not in {"plugin-wiring", "config"}]
    clis = [c for c in checks if c["name"].startswith("cli:")]
    found = [c["name"][4:] for c in clis if c["status"] == "ok"]
    if found:
        out(_line("ok", f"CLIs found: {', '.join(found)}"))
        for c in clis:
            if c["status"] != "ok":
                out(_line("skip", f"{c['name'][4:]} not installed (optional)"))
    else:
        out(_line("fail", "no provider CLI found"))
        failed = True
    rest = [c for c in checks if not c["name"].startswith("cli:")]
    tools = [c for c in rest if c["name"] in {"bubblewrap", "git", "systemd-run"} and c["status"] == "ok"]
    if tools:
        out(_line("ok", ", ".join(c["name"] for c in tools)))
    for c in rest:
        if c in tools:
            continue
        if c["name"] == "quota-cache" and c["status"] == "warn" and "not found" in c["hint"]:
            out(_line("skip", "quota cache fills in after the first task"))
            continue
        out(_line(c["status"], f"{c['name']}: {c['hint']}"))
        failed |= c["status"] == "fail"

    out("")
    out("Next:")
    out("  1. hermes gateway restart")
    out('  2. Ask Hermes: "route a task: fix the failing test in /path/to/repo"')
    out("")
    out("Safe to re-run. It never overwrites your cli-router.yaml.")
    out("`hermes cli-router setup --check` creates and changes no config; it only reports.")
    return 1 if failed else 0


def setup_parser(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="cli_router_command", required=True)
    setup = sub.add_parser("setup", help="Create cli-router.yaml and check this machine",
                           description="Create cli-router.yaml if absent and check this machine.")
    setup.add_argument("--check", action="store_true", help="only report; change nothing")


def handle(args: argparse.Namespace) -> None:
    sys.exit(run_setup(check_only=bool(getattr(args, "check", False))))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="setup_command.py")
    parser.add_argument("--check", action="store_true")
    return run_setup(check_only=parser.parse_args(argv).check)


if __name__ == "__main__":
    sys.exit(main())
