"""Jev handoff retries and deterministic continuation when assessment fails."""
import sys
import types
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import task_manager as tm


ROUTES = [
    {"cli": "codex", "model": "m1", "effort": "low"},
    {"cli": "kimi", "model": "m2", "effort": "low"},
]


class Context:
    def get_config(self, name, default=None):
        return {"backend": "openrouter", "model": "typesafe/jev-1.14"}


def run_worker(tmp_path, service_run, changed_files):
    task_id = "e" * 32
    repo = tmp_path / "repo"
    repo.mkdir()
    ref = {"workstream_id": str(uuid.UUID(task_id)), "latest_sequence": 1}
    calls = []

    class Service:
        def __init__(self, ctx):
            pass

        def run(self, request):
            calls.append(request)
            return service_run(request)

    fake = types.ModuleType("hermes_jev.service")
    fake.Service = Service
    checks = {"checks": {"diff": {"ok": False}}, "changed_files": changed_files,
              "head_unchanged": True}
    with patch.object(tm, "_root", return_value=tmp_path / "home"), \
         patch.object(tm, "_lock_path", return_value=tmp_path / "repo.lock"), \
         patch.object(tm, "_candidate_has_quota", return_value=True), \
         patch.object(tm, "_git_readonly", return_value=SimpleNamespace(stdout="head")), \
         patch.object(tm, "_git_changed_paths", return_value=[]), \
         patch.object(tm, "_path_digests", return_value={}), \
         patch.object(tm, "_independent_checks", return_value=checks), \
         patch.object(tm.ai_memory_bridge, "verify_runtime"), \
         patch.object(tm.ai_memory_bridge, "read_context",
                      return_value=(f"Code task {task_id} attempt 1", 1)), \
         patch.object(tm, "_run_candidate", side_effect=[
             {"outcome": "quota_limited", "ai_memory": ref},
             {"outcome": "account_stop", "ai_memory": ref},
         ]) as worker, \
         patch.object(tm.hermes_adapter, "_CONTEXT", Context()), \
         patch.dict("sys.modules", {"hermes_jev.service": fake}):
        tm._write(task_id, {"task_id": task_id, "state": "queued"})
        tm._worker(task_id, repo, "task", "change", [], ROUTES, None)
        data = tm._read(task_id)
    return data, worker, calls


def test_retries_after_failed_request_then_uses_real_jev_answer_without_fallback(tmp_path):
    attempts = 0

    def service_run(request):
        nonlocal attempts
        attempts += 1
        if attempts < 2:
            return {"ok": False}
        return {"ok": True, "answers": {"handoff": {"choice": "continue"}}}

    data, worker, calls = run_worker(tmp_path, service_run, ["changed.py"])
    assert len(calls) == 2
    assert worker.call_count == 2
    assert data["ai_memory"]["handoff_decision"] == "continue"
    assert not data.get("route_warnings")


def test_persistent_failure_continues_on_progress_and_records_warning(tmp_path):
    def service_run(request):
        raise RuntimeError("service unavailable")

    data, worker, calls = run_worker(tmp_path, service_run, ["changed.py"])
    assert len(calls) == 2
    assert worker.call_count == 2
    assert data["ai_memory"]["handoff_decision"] == "continue"
    expected = "Jev handoff failed (RuntimeError); deterministic continuation used"
    assert expected in data["route_warnings"]
    assert any(event.get("event") == "route_warning" and event.get("message") == expected
               for event in data["events"])
    assert any(event.get("event") == "handoff_assessed" and event.get("outcome") == "continue"
               for event in data["events"])


def test_persistent_failure_stops_without_progress_with_unsuitable_message(tmp_path):
    def service_run(request):
        raise RuntimeError("service unavailable")

    data, worker, calls = run_worker(tmp_path, service_run, [])
    assert len(calls) == 2
    assert worker.call_count == 1
    assert data["state"] == "blocked"
    assert data["message"] == "Jev judged the persisted checkpoint unsuitable for another attempt"
    assert data["ai_memory"]["handoff_decision"] == "stop"
    assert "could not assess" not in data["message"]


def test_unknown_choice_uses_deterministic_progress_fallback(tmp_path):
    def service_run(request):
        return {"ok": True, "answers": {"handoff": {"choice": "unknown"}}}

    data, worker, calls = run_worker(tmp_path, service_run, ["changed.py"])
    assert len(calls) == 1
    assert worker.call_count == 2
    assert data["ai_memory"]["handoff_decision"] == "continue"
    assert data["route_warnings"] == [
        "Jev handoff failed (RuntimeError); deterministic continuation used"
    ]
