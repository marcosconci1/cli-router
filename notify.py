"""Pure builders for compact Hermes task notifications."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _one_line(value: Any) -> str:
    return " ".join(str(value or "").split())


def post_error(task_view: dict[str, Any]) -> str:
    task_id = str(task_view.get("task_id") or "")
    label = _one_line(task_view.get("label")) or task_id[:8] or "task"
    degraded = task_view.get("degraded")
    first = degraded[0] if isinstance(degraded, list) and degraded and isinstance(degraded[0], dict) else {}
    code = _one_line(first.get("code")) or _one_line(task_view.get("error_class")) or "TASK_FAILED"
    component = _one_line(first.get("component")) or "router"
    cause = _one_line(first.get("detail") or task_view.get("message") or task_view.get("error_detail"))
    # Use the router's canonical credential masking for every cause before display.
    try:
        try:
            from . import task_manager
        except ImportError:  # plain-module layout (tests, CLI probes)
            import task_manager
        cause = task_manager._safe_text(cause, 180)
    except Exception:
        cause = "sensitive failure detail redacted"
    cause = cause[:180] or "task ended without a successful result"
    return f"⚠️ cli-router {label}: {component}/{code} — {cause} · fix: docs/troubleshooting.md#{code}"


def progress(task_view: dict[str, Any], now: datetime | None = None) -> str:
    task_id = str(task_view.get("task_id") or "")[:8] or "task"
    attempt = task_view.get("current_attempt") or 0
    phase = _one_line(task_view.get("phase")) or _one_line(task_view.get("state")) or "working"
    route = task_view.get("route") if isinstance(task_view.get("route"), dict) else {}
    cli, model = route.get("cli"), route.get("model")
    if not cli and isinstance(task_view.get("attempts"), list) and task_view["attempts"]:
        cli = task_view["attempts"][-1].get("cli")
        model = task_view["attempts"][-1].get("model")
    worker = _one_line(f"{cli or 'unknown'}/{model or 'unknown'}")
    review = task_view.get("verification", {}).get("external", {}).get("reviews", []) if isinstance(task_view.get("verification"), dict) else []
    review_part = ""
    if isinstance(review, list) and review and isinstance(review[-1], dict):
        candidate = review[-1].get("route") or review[-1]
        if isinstance(candidate, dict):
            review_part = f" → review {_one_line(candidate.get('cli') or 'unknown')}/{_one_line(candidate.get('model') or 'unknown')}"
    try:
        start = datetime.fromisoformat(str(task_view.get("started_at") or task_view.get("created_at")))
        elapsed = max(0, int(((now or datetime.now(timezone.utc)) - start).total_seconds()))
    except (TypeError, ValueError):
        elapsed = 0
    return f"⚙️ cli-router {task_id} · attempt {attempt} · {phase} · worker {worker}{review_part} · {elapsed}s"
