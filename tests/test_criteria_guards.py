"""Guards for unsatisfiable acceptance criteria: submit validation, circuit breaker, reasons, pin echo."""
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import task_manager as tm

ROUTES = [{"cli": "claude", "model": f"m{i}", "effort": "low"} for i in range(3)]


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo


def _submit(tmp_path, mode, criteria, extra=None):
    repo = _repo(tmp_path)
    (repo / "present.md").write_text("x")
    started = []

    class FakeThread:
        def __init__(self, *a, **k):
            pass

        def start(self):
            started.append(1)

    with patch.object(tm, "_root", return_value=tmp_path / "home"), \
         patch.object(tm, "_repo", side_effect=lambda p: Path(p).resolve()), \
         patch.object(tm, "_repo_key", return_value="k" * 24), \
         patch.object(tm, "_lock_path", return_value=tmp_path / "repo.lock"), \
         patch.object(tm.ai_memory_bridge, "verify_runtime"), \
         patch.object(tm, "_quota", return_value={"fresh": True, "cache": {}}), \
         patch.object(tm, "_subscriptions", return_value=ROUTES), \
         patch.object(tm, "_installed_models", return_value={}), \
         patch.object(tm, "_worker_capable", side_effect=lambda c: c), \
         patch.object(tm, "_jev_choose", side_effect=AssertionError("no jev")), \
         patch.object(tm, "_free_models", return_value=[]), \
         patch.object(tm.threading, "Thread", FakeThread):
        result = tm.dispatch({"action": "submit", "repository": str(repo), "task": "t", "mode": mode,
                              "acceptance_criteria": criteria, **(extra or {})})
    return result, tmp_path / "home"


def _task_files(home):
    return list(home.rglob("*.json")) if home.exists() else []


def test_analysis_missing_file_exists_rejected_without_record(tmp_path):
    result, home = _submit(tmp_path, "analysis", [{"type": "file_exists", "path": "docs/x.md"}])
    assert result["ok"] is False
    assert "analysis" in result["error"] and "docs/x.md" in result["error"]
    assert _task_files(home) == []


def test_analysis_existing_file_exists_valid(tmp_path):
    result, _ = _submit(tmp_path, "analysis", [{"type": "file_exists", "path": "present.md"}], {"cli": "claude"})
    assert result.get("state") == "queued"


def test_analysis_file_changed_rejected(tmp_path):
    result, _ = _submit(tmp_path, "analysis", [{"type": "file_changed", "path": "present.md"}])
    assert result["ok"] is False and "file_changed" in result["error"]


def test_change_missing_file_exists_valid(tmp_path):
    result, _ = _submit(tmp_path, "change", [{"type": "file_exists", "path": "docs/x.md"}],
                       {"cli": "claude"})
    assert result.get("state") == "queued"


def test_route_pin_echoed(tmp_path):
    result, _ = _submit(tmp_path, "analysis", [{"type": "git_diff_clean"}], {"cli": "claude"})
    assert result["route_pin"] == {"cli": "claude", "model": None}


def _run_worker(tmp_path, mode, criteria, check_sequence, review=None):
    """check_sequence: per-attempt changed_files lists; criteria evaluated for real."""
    task_id = uuid.uuid4().hex
    repo = _repo(tmp_path)
    ref = {"workstream_id": str(uuid.UUID(task_id)), "latest_sequence": 1}
    changed = list(check_sequence)

    def checks(*a, **k):
        return {"checks": {}, "changed_files": changed.pop(0), "head_unchanged": True}

    run = patch.object(tm, "_run_candidate",
                       return_value={"outcome": "needs_verification", "ai_memory": ref})
    with patch.object(tm, "_root", return_value=tmp_path / "home"), \
         patch.object(tm, "_lock_path", return_value=tmp_path / "repo.lock"), \
         patch.object(tm, "_candidate_has_quota", return_value=True), \
         patch.object(tm, "_ai_memory_mode", return_value="enabled"), \
         patch.object(tm, "_git_readonly", return_value=SimpleNamespace(stdout="head")), \
         patch.object(tm, "_work_tree_root", side_effect=lambda r: Path(r).resolve()), \
         patch.object(tm, "_git_changed_paths", return_value=[]), \
         patch.object(tm, "_path_digests", return_value={}), \
         patch.object(tm, "_independent_checks", side_effect=checks), \
         patch.object(tm, "_independent_review_enabled", return_value=True), \
         patch.object(tm, "_external_verification",
                      return_value=review or {"passed": False, "failed": True, "summary": "VERDICT: FAIL",
                                              "reviews": []}), \
         patch.object(tm, "_jev_handoff", return_value=True), \
         patch.object(tm, "_notify_error_if_needed") as notify, \
         patch.object(tm.ai_memory_bridge, "verify_runtime"), \
         patch.object(tm.ai_memory_bridge, "read_context_with_wait", return_value=("ctx", 1)), \
         patch.object(tm.ai_memory_bridge, "read_context", return_value=("ctx", 1)), \
         run as worker:
        tm._write(task_id, {"task_id": task_id, "state": "queued"})
        tm._worker(task_id, repo, "task", mode, criteria, ROUTES, None)
        return tm._read(task_id), worker, notify


MISSING = [{"type": "file_exists", "path": "docs/x.md"}]


def test_breaker_trips_after_two_identical_local_failures(tmp_path):
    data, worker, notify = _run_worker(tmp_path, "analysis", MISSING, [[], [], []])
    assert worker.call_count == 2
    assert data["state"] == "blocked"
    ev = [e for e in data["events"] if e["event"] == "criteria_unsatisfiable"]
    assert ev and data["phase"] == "blocked"
    assert "docs/x.md" in data["message"] and "read-only" in data["message"]
    assert data.get("finished_at")
    notify.assert_called_once()


def test_breaker_ignores_differing_signature_and_external_fail(tmp_path):
    # Differing signatures: a missing file on attempt 1, a different failure on attempt 2.
    repo_crit = [{"type": "file_exists", "path": "a.md"}, {"type": "file_exists", "path": "b.md"}]
    sigs = iter([{"criteria": [{"criterion": "file_exists", "path": "a.md", "ok": False, "reason": "r"}],
                  "passed": False},
                 {"criteria": [{"criterion": "file_exists", "path": "b.md", "ok": False, "reason": "r"}],
                  "passed": False},
                 {"criteria": [{"criterion": "file_exists", "path": "c.md", "ok": False, "reason": "r"}],
                  "passed": False}])
    with patch.object(tm, "_acceptance", side_effect=lambda *a, **k: next(sigs)):
        data, worker, _ = _run_worker(tmp_path, "analysis", repo_crit, [[], [], []])
    assert worker.call_count == 3
    assert not [e for e in data["events"] if e["event"] == "criteria_unsatisfiable"]


def test_breaker_not_tripped_when_first_failure_is_external_review(tmp_path):
    ok_then_missing = iter([
        {"criteria": [{"criterion": "file_exists", "path": "docs/x.md", "ok": True, "reason": "ok"}],
         "passed": True},
        {"criteria": [{"criterion": "file_exists", "path": "docs/x.md", "ok": False, "reason": "m"}],
         "passed": False},
        {"criteria": [{"criterion": "file_exists", "path": "docs/x.md", "ok": False, "reason": "m"}],
         "passed": False}])
    with patch.object(tm, "_acceptance", side_effect=lambda *a, **k: next(ok_then_missing)):
        # attempt 1 passes local checks (analysis, no changes) -> external FAIL
        data, worker, _ = _run_worker(tmp_path, "analysis", MISSING, [[], [], []])
    # Attempt 2 is the first local failure, attempt 3 the second: breaker may only trip then.
    assert worker.call_count >= 2
    first = data["attempts"][0]
    assert first["outcome"] == "failed_verification"
    assert not first["reason"].startswith("acceptance failed")


def test_attempt_reason_names_failing_criterion(tmp_path):
    data, _, _ = _run_worker(tmp_path, "analysis", MISSING, [[], []])
    assert data["attempts"][0]["reason"] == "acceptance failed: file_exists docs/x.md (file is missing)"
