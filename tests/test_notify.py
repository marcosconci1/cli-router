from datetime import datetime, timezone
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import notify


def test_error_line_component_classes_and_secret_patterns():
    for component, code in (("Jev", "JEV_REQUEST_FAILED"), ("CLI", "CLI_AUTH"),
                           ("CLI", "CLI_BILLING"), ("CLI", "CLI_QUOTA")):
        line = notify.post_error({"task_id": "12345678", "degraded": [
            {"component": component, "code": code, "detail": "unavailable"}]})
        assert f"{component}/{code}" in line
    line = notify.post_error({"task_id": "12345678", "error_detail": "API_KEY=sk-abcdefghijklmnopqrstuvwxyz"})
    assert "sk-" not in line


def test_error_builder_never_includes_task_prompt():
    line = notify.post_error({"task_id": "12345678", "task": "private prompt content",
                              "error_detail": "safe cause"})
    assert "private prompt" not in line


def test_error_line_has_component_code_and_anchor():
    line = notify.post_error({"task_id": "12345678abcdef", "label": "demo",
                              "degraded": [{"component": "ai-memory", "code": "AIM_AUTH", "detail": "login expired"}]})
    assert line == "⚠️ cli-router demo: ai-memory/AIM_AUTH — login expired · fix: docs/troubleshooting.md#AIM_AUTH"


def test_error_line_falls_back_to_router_and_redacts_synthetic_secret():
    line = notify.post_error({"task_id": "12345678", "error_detail": "AI_MEMORY_AUTH_TOKEN=[redacted]"})
    assert "router/TASK_FAILED" in line
    assert "AI_MEMORY_AUTH_TOKEN" not in line


def test_progress_line_includes_worker_review_phase_and_elapsed():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    line = notify.progress({"task_id": "1234567890", "current_attempt": 2, "phase": "review",
                            "started_at": start.isoformat(), "route": {"cli": "codex", "model": "m"},
                            "verification": {"external": {"reviews": [{"cli": "agy", "model": "r"}]}}},
                           now=datetime(2026, 1, 1, 0, 0, 8, tzinfo=timezone.utc))
    assert "12345678 · attempt 2 · review · worker codex/m → review agy/r · 8s" in line
