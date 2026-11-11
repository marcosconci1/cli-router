"""Local diagnostic journal and redacted diagnose bundle.

Everything here stays on this machine: no telemetry and no network calls. The journal is an
append-only JSON Lines file under the task directory with hardcoded retention (30 days, 1 MB).
The diagnose bundle is a plain-text report that omits prompts, repository paths, owner tokens,
and command lines.
"""
from __future__ import annotations

import json
import os
import platform
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

JOURNAL_NAME = "events.log.jsonl"
RETENTION_DAYS = 30
MAX_JOURNAL_BYTES = 1024 * 1024
TAIL_LINES = 50
RECENT_TASKS = 10
_TASK_ID = re.compile(r"[0-9a-f]{32}")
_ABSOLUTE_PATH = re.compile(r"(?<![\w.:])/(?:[\w.@+~-]+/)*[\w.@+~-]*")
_LOCK = threading.Lock()


def _safe_text(value: Any, limit: int = 1000) -> str:
    # Reuse task_manager's secret masking; imported lazily because task_manager imports this module.
    try:
        from . import task_manager
    except ImportError:
        import task_manager
    return task_manager._safe_text(value, limit)


def _scrub_paths(text: str) -> str:
    return _ABSOLUTE_PATH.sub("[path]", text)


def _directory(home: str | os.PathLike[str]) -> Path:
    path = Path(home) / "runtime" / "cli-tasks"
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def journal_path(home: str | os.PathLike[str]) -> Path:
    return _directory(home) / JOURNAL_NAME


def _sanitize(value: Any) -> Any:
    if isinstance(value, str):
        return _safe_text(value)
    if isinstance(value, dict):
        return {str(key): _sanitize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _safe_text(value)


def _entry_time(line: str) -> datetime | None:
    try:
        entry = json.loads(line)
        stamp = datetime.fromisoformat(entry["at"])
    except (ValueError, TypeError, KeyError):
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def trim_lines(lines: list[str], now: datetime | None = None,
               max_age_days: int = RETENTION_DAYS, max_bytes: int = MAX_JOURNAL_BYTES) -> list[str]:
    """Pure retention: keep entries newer than max_age_days, then drop oldest until within max_bytes.

    Lines that are not valid journal entries (no parseable "at") are dropped.
    """
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=max_age_days)
    kept = [line for line in lines if (stamp := _entry_time(line)) is not None and stamp >= cutoff]
    total = sum(len(line.encode("utf-8")) + 1 for line in kept)
    drop = 0
    while drop < len(kept) and total > max_bytes:
        total -= len(kept[drop].encode("utf-8")) + 1
        drop += 1
    return kept[drop:]


def trim_journal(home: str | os.PathLike[str], now: datetime | None = None) -> None:
    path = journal_path(home)
    with _LOCK:
        _trim_locked(path, now)


def _trim_locked(path: Path, now: datetime | None = None) -> None:
    try:
        lines = [line for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
    except FileNotFoundError:
        return
    kept = trim_lines(lines, now)
    if kept == lines:
        return
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write("".join(line + "\n" for line in kept))
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def append_event(home: str | os.PathLike[str], kind: str, payload: dict[str, Any]) -> None:
    """Append one sanitized JSON line; raises on failure so callers decide how to degrade."""
    entry = {"at": datetime.now(timezone.utc).isoformat(), "kind": kind, **_sanitize(payload)}
    line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
    path = journal_path(home)
    with _LOCK:
        _trim_locked(path)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as stream:
            stream.write(line)


def _version() -> str:
    try:
        text = (Path(__file__).resolve().parent / "plugin.yaml").read_text(encoding="utf-8")
    except OSError:
        return "unknown"
    match = re.search(r"(?m)^version:\s*['\"]?([^\s'\"#]+)", text)
    return match.group(1) if match else "unknown"


def _journal_tail(path: Path) -> list[str]:
    try:
        lines = [line for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
    except OSError:
        return []
    tail = []
    for line in lines[-TAIL_LINES:]:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        entry.pop("repository", None)
        tail.append(_scrub_paths(_safe_text(json.dumps(entry, ensure_ascii=False, separators=(",", ":")), 2000)))
    return tail


def _degraded_codes(path: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    try:
        lines = [line for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
    except OSError:
        return counts
    for line in lines[-TAIL_LINES:]:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("kind") == "degraded":
            code = _safe_text(entry.get("code"), 60)
            if re.fullmatch(r"[A-Z_]+", code):
                counts[code] = counts.get(code, 0) + 1
    return counts


def _recent_tasks(directory: Path) -> list[dict[str, str]]:
    candidates = []
    for path in directory.glob("*.json"):
        if _TASK_ID.fullmatch(path.stem):
            try:
                candidates.append((path.stat().st_mtime, path))
            except OSError:
                continue
    tasks = []
    for _, path in sorted(candidates, key=lambda item: item[0], reverse=True)[:RECENT_TASKS]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        tasks.append({key: _safe_text(data.get(key), 80) for key in
                      ("task_id", "state", "phase", "updated_at", "error_class")})
    return tasks


def build_report(home: str | os.PathLike[str], checks: list[dict[str, Any]]) -> tuple[Path, str]:
    """Write and return the redacted diagnose bundle."""
    directory = _directory(home)
    now = datetime.now(timezone.utc)
    lines = [f"cli-router diagnose bundle {now.isoformat()}",
             f"router version: {_safe_text(_version(), 40)}",
             f"python: {platform.python_version()}",
             f"platform: {_safe_text(platform.platform(), 120)}",
             "", "doctor checks:"]
    for check in checks:
        lines.append(f"  {_safe_text(check.get('name'), 80)}: {_safe_text(check.get('status'), 20)}")
    lines += ["", f"journal tail (last {TAIL_LINES}):"]
    lines += [f"  {entry}" for entry in _journal_tail(directory / JOURNAL_NAME)]
    lines += ["", "degraded codes in journal window:"]
    degraded = _degraded_codes(directory / JOURNAL_NAME)
    lines += [f"  {code} x{count}" for code, count in degraded.items()] or ["  none"]
    lines += ["", f"recent tasks (up to {RECENT_TASKS}):"]
    for task in _recent_tasks(directory):
        lines.append("  " + " ".join(f"{key}={task[key] or '-'}" for key in task))
    report = "\n".join(lines) + "\n"
    path = directory / f"diagnose-{now.strftime('%Y%m%dT%H%M%S%fZ')}.txt"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(report)
    return path, report
