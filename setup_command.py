"""One-command first run: `hermes cli-router setup [--check]`."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable

try:
    from . import doctor, problems
except ImportError:
    import doctor
    import problems

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


def _config_values(path: Path) -> dict[str, Any]:
    """Read just the choice values from the existing, validated config text."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return {}
    values: dict[str, Any] = {}
    in_section = False
    for raw in lines:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        line = raw.strip()
        if indent == 0:
            in_section = line == "cli-router:"
            continue
        if in_section and indent == 2 and ":" in line:
            key, value = line.split(":", 1)
            value = value.strip()
            values[key.strip()] = {"true": True, "false": False}.get(value, value)
    return values


def _render_config(jev: bool | None, ai_memory: str | None) -> bytes:
    text = (_ROOT / "cli-router.example.yaml").read_text(encoding="utf-8")
    if jev is not None:
        text = text.replace("  # jev: true\n", f"  jev: {'true' if jev else 'false'}\n")
    if ai_memory is not None:
        text = text.replace("  # ai-memory: auto\n", f"  ai-memory: {ai_memory}\n")
    return text.encode("utf-8")


def _existing_config(home: Path | None) -> Path | None:
    try:
        configured = doctor._task_manager_module()._config_path()
    except Exception:
        configured = None
    candidates = [configured] if configured is not None else []
    if home is not None:
        candidates.append(home / "cli-router.yaml")
    return next((path for path in candidates if path.is_file()), None)


def _create_config(home: Path | None, check_only: bool, jev: bool | None,
                   ai_memory: str | None) -> tuple[str, str]:
    module = doctor._task_manager_module()
    try:
        existing = module._config_path()
    except Exception as exc:
        return "fail", f"invalid CLI_ROUTER_CONFIG: {exc}"
    candidates = [existing] if existing is not None else []
    if home is not None:
        candidates.append(home / "cli-router.yaml")
    present = next((p for p in candidates if p.is_file()), None)
    if present is not None:
        return "ok", "cli-router.yaml present (kept as is)"
    if home is None:
        return "fail", "hermes home could not be located; set HERMES_HOME"
    if check_only:
        return "skip", "cli-router.yaml not created (--check)"
    try:
        home.mkdir(parents=True, exist_ok=True)
        (home / "cli-router.yaml").write_bytes(_render_config(jev, ai_memory))
    except OSError as exc:
        return "fail", f"could not create {home / 'cli-router.yaml'}: {exc}"
    return "new", f"created {home / 'cli-router.yaml'} (defaults; edit to taste)"


def _prompt_choices(jev: str | None, ai_memory: str | None, no_input: bool,
                    check_only: bool, creating: bool, json_output: bool = False) -> tuple[bool | None, str | None]:
    if no_input or check_only or not creating or not sys.stdin.isatty():
        return (True if jev == "on" else False if jev == "off" else None,
                ai_memory)
    def ask(prompt: str) -> str:
        if not json_output:
            return input(prompt)
        sys.stderr.write(prompt)
        return sys.stdin.readline()

    if jev is None:
        answer = ask("Use Jev for route ranking? [y/N] ").strip().lower()
        if answer in {"y", "yes"}:
            jev = "on"
        elif answer in {"n", "no"}:
            jev = "off"
    if ai_memory is None:
        answer = ask("Use ai-memory? [auto/enabled/disabled] (Enter = skip) ").strip().lower()
        if answer in {"auto", "enabled", "disabled"}:
            ai_memory = answer
    return (True if jev == "on" else False if jev == "off" else None, ai_memory)


def _sanitize(value: Any, home: Path | None) -> str:
    try:
        from . import task_manager
    except ImportError:
        import task_manager
    safe = task_manager._safe_text(str(value), 2000)
    if home is not None:
        safe = safe.replace(str(home), "~")
    return safe


def _record(name: str, status: str, hint: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "status": status, "hint": hint, **extra}


def run_setup(home: Any = None, check_only: bool = False, out: Callable[[str], Any] = print,
              *, ai_memory: str | None = None, jev: str | None = None,
              no_input: bool = False, strict: bool = False, json_output: bool = False,
              terminal: bool | None = None) -> int:
    resolved = doctor._resolve_home(home)
    if terminal is not None:
        is_terminal = terminal
    else:
        is_terminal = sys.stdin.isatty()
    if check_only and (ai_memory is not None or jev is not None):
        raise ValueError("--check cannot be combined with --ai-memory or --jev")
    config_target = (resolved / "cli-router.yaml") if resolved else None
    existing_config = _existing_config(resolved)
    creating = config_target is not None and existing_config is None
    jev_choice, ai_memory = _prompt_choices(jev, ai_memory, no_input, check_only,
                                            creating and is_terminal, json_output)

    records: list[dict[str, Any]] = []
    def emit(tag: str, text: str, name: str, hint: str | None = None, **extra: Any) -> None:
        records.append(_record(name, tag, hint if hint is not None else text, **extra))
        if not json_output:
            out(_line(tag, text))

    if not json_output:
        out(f"cli-router {_version()} · profile: {resolved.name if resolved else 'unknown'}")

    wiring = doctor._check_plugin_wiring(resolved)
    emit(wiring["status"], "plugin enabled" if wiring["status"] == "ok" else
         f"plugin wiring: {wiring['hint']}", "plugin-wiring", wiring["hint"])

    platforms = _toolset_platforms(resolved)
    if platforms is None:
        emit("ok", "tool reachable on: all platforms (Hermes default)", "tools-reachable",
             "all platforms (Hermes default)")
    elif platforms:
        emit("ok", f"tool reachable on: {', '.join(platforms)}", "tools-reachable",
             f"{', '.join(platforms)}")
    else:
        emit("warn", "tool reachable on: no platform; add cli_router to platform_toolsets",
             "tools-reachable", "no platform; add cli_router to platform_toolsets")

    tag, config_hint = _create_config(resolved, check_only, jev_choice, ai_memory)
    emit(tag, config_hint, "config", config_hint)
    flags = (("--jev", jev, "jev"), ("--ai-memory", ai_memory, "ai-memory"))
    conflict_warnings: list[tuple[str, str]] = []
    if tag == "ok" and existing_config is not None:
        current = _config_values(existing_config)
        desired = {"jev": {"on": True, "off": False}.get(jev) if jev is not None else None,
                   "ai-memory": ai_memory}
        for flag, requested, key in flags:
            if requested is not None and current.get(key, object()) != desired[key]:
                display = requested
                setting = f"{key}: {str(desired[key]).lower() if isinstance(desired[key], bool) else desired[key]}"
                warn = f"cli-router.yaml already exists; {flag} {display} not applied (set `{setting}` in the file)"
                conflict_warnings.append((key, warn))

    effective_jev = jev_choice if tag == "new" else None
    if tag == "ok" and existing_config is not None:
        values = _config_values(existing_config)
        effective_jev = values.get("jev") if isinstance(values.get("jev"), bool) else None
        value = values.get("ai-memory")
        ai_memory = value if value in {"auto", "enabled", "disabled"} else None
    elif tag != "new":
        ai_memory = None
        effective_jev = None
    choice_bits = []
    if ai_memory is not None:
        choice_bits.append(f"ai-memory {ai_memory}")
    if effective_jev is not None:
        choice_bits.append(f"Jev {'on' if effective_jev else 'off'}")
    if choice_bits:
        choice_text = "choices recorded: " + ", ".join(choice_bits)
        choice_status = "ok"
    else:
        choice_text = "choices: none recorded (Jev and ai-memory not chosen)"
        choice_status = "skip"
    emit(choice_status, choice_text, "choices", choice_text)
    for key, warning in conflict_warnings:
        emit("warn", warning, f"choice-{key}", warning)

    checks = [c for c in doctor.run_checks(resolved) if c["name"] not in {"plugin-wiring", "config"}]
    clis = [c for c in checks if c["name"].startswith("cli:")]
    found = [c["name"][4:] for c in clis if c["status"] == "ok"]
    if found:
        emit("ok", f"CLIs found: {', '.join(found)}", "provider-clis", f"installed: {', '.join(found)}")
        for c in clis:
            if c["status"] != "ok":
                emit("skip", f"{c['name'][4:]} not installed (optional)", c["name"], c["hint"])
    else:
        emit("fail", "no provider CLI found", "provider-clis", "no provider CLI found")

    rest = [c for c in checks if not c["name"].startswith("cli:")]
    tools = [c for c in rest if c["name"] in {"bubblewrap", "git", "systemd-run"} and c["status"] == "ok"]
    if tools:
        emit("ok", ", ".join(c["name"] for c in tools), "tools", ", ".join(c["name"] for c in tools))
    for c in rest:
        if c in tools:
            continue
        if c["name"] == "quota-cache" and c["status"] == "warn" and "not found" in c["hint"]:
            emit("skip", "quota cache fills in after the first task", c["name"], c["hint"])
            continue
        extra = {key: c[key] for key in ("chosen", "code") if key in c}
        if c.get("chosen") and c.get("status") != "ok" and c.get("code") in problems.CODES:
            spec = problems.CODES[c["code"]]
            extra.update(fix=spec["fix"], docs="docs/troubleshooting.md#" + c["code"].lower())
        emit(c["status"], f"{c['name']}: {c['hint']}", c["name"], c["hint"], **extra)
        if not json_output and extra.get("fix"):
            out(f"        fix: {extra['fix']}")
            out(f"        docs: {extra['docs']}")

    failed = any(r["status"] == "fail" or (strict and r["status"] == "warn") for r in records)
    exit_code = 1 if failed else 0
    if json_output:
        for record in records:
            record["hint"] = _sanitize(record["hint"], resolved)
            for key in ("fix", "docs"):
                if key in record:
                    record[key] = _sanitize(record[key], resolved)
        payload = {"version": _version(), "profile": resolved.name if resolved else "unknown",
                   "ok": exit_code == 0, "strict": bool(strict), "exit_code": exit_code,
                   "choices": {"ai-memory": ai_memory or "unset",
                               "jev": effective_jev if effective_jev is not None else "unset"},
                   "checks": records}
        out(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    else:
        out("")
        out("Next:")
        out('  1. hermes gateway restart')
        out('  2. Ask Hermes: "route a task: fix the failing test in /path/to/repo"')
        out("")
        out("Safe to re-run. It never overwrites your cli-router.yaml.")
        out("`hermes cli-router setup --check` creates and changes no config; it only reports.")
    return exit_code


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.check and (args.ai_memory is not None or args.jev is not None):
        parser.error("--check cannot be combined with --ai-memory or --jev")


class _SetupParser(argparse.ArgumentParser):
    def parse_known_args(self, args=None, namespace=None):
        parsed, extras = super().parse_known_args(args, namespace)
        if getattr(parsed, "check", False) and (getattr(parsed, "ai_memory", None) is not None or
                                                  getattr(parsed, "jev", None) is not None):
            self.error("--check cannot be combined with --ai-memory or --jev")
        return parsed, extras


def setup_parser(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="cli_router_command", required=True, parser_class=_SetupParser)
    setup = sub.add_parser("setup", help="Create cli-router.yaml and check this machine",
                           description="Create cli-router.yaml if absent and check this machine.",
                           epilog="Exit codes: 0 no failed check (warnings allowed); 1 a failed check or a warning with --strict; 2 usage error.")
    setup.add_argument("--check", action="store_true", help="only report; change nothing")
    setup.add_argument("--ai-memory", choices=("auto", "enabled", "disabled"), help="record the ai-memory choice")
    setup.add_argument("--jev", choices=("on", "off"), help="record the Jev choice")
    setup.add_argument("--no-input", action="store_true", help="never prompt for choices")
    setup.add_argument("--strict", action="store_true", help="treat warnings as failures")
    setup.add_argument("--json", action="store_true", dest="json_output", help="print a JSON result only")
    setup.set_defaults(_setup_parser=setup)


def handle(args: argparse.Namespace) -> None:
    if hasattr(args, "_setup_parser"):
        _validate_args(args._setup_parser, args)
    sys.exit(run_setup(check_only=bool(getattr(args, "check", False)),
                       ai_memory=getattr(args, "ai_memory", None), jev=getattr(args, "jev", None),
                       no_input=bool(getattr(args, "no_input", False)),
                       strict=bool(getattr(args, "strict", False)),
                       json_output=bool(getattr(args, "json_output", False))))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hermes cli-router setup",
                                     description="Create cli-router.yaml and check this machine.",
                                     epilog="Exit codes: 0 no failed check (warnings allowed); 1 a failed check or a warning with --strict; 2 usage error.")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--ai-memory", choices=("auto", "enabled", "disabled"))
    parser.add_argument("--jev", choices=("on", "off"))
    parser.add_argument("--no-input", action="store_true")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv)
    _validate_args(parser, args)
    return run_setup(check_only=args.check, ai_memory=args.ai_memory, jev=args.jev,
                     no_input=args.no_input, strict=args.strict, json_output=args.json_output)


if __name__ == "__main__":
    sys.exit(main())
