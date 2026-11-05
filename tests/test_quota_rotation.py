"""Quota rotation regressions; provider calls are isolated at the worker seam."""
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import task_manager as tm


def test_review_skips_reserved_route_and_reselects_two_models():
    routes = [{"cli": cli, "model": cli, "effort": None} for cli in ("codex", "kimi", "agy")]
    with tempfile.TemporaryDirectory() as temp:
        repo = Path(temp)
        with patch.object(tm, "_jev_choose", side_effect=lambda task, cs, **kw: cs[:2]), \
             patch.object(tm, "_candidate_has_quota", side_effect=lambda c: c["cli"] != "codex"), \
             patch.object(tm, "_run_candidate", return_value={"outcome": "needs_verification", "summary": "VERDICT: PASS"}) as run:
            result = tm._external_verification("a" * 32, repo, "review", [], routes, routes[0], None, 2)
    assert [c.args[0]["cli"] for c in run.call_args_list] == ["kimi", "agy"]
    assert result["passed"]


@pytest.mark.parametrize("memory_ok", [True, False])
def test_review_rotates_quota_failure_only_with_confirmed_memory(memory_ok):
    routes = [{"cli": cli, "model": cli, "effort": None} for cli in ("codex", "kimi", "agy")]
    task_id = "b" * 32
    memory = {"workstream_id": "stream", "latest_sequence": 1}
    outcomes = [
        {"outcome": "quota_limited", "reason": "HTTP 429 quota limit", "ai_memory": memory if memory_ok else None},
        {"outcome": "needs_verification", "summary": "VERDICT: PASS", "ai_memory": memory},
        {"outcome": "needs_verification", "summary": "VERDICT: PASS", "ai_memory": memory},
    ]
    with tempfile.TemporaryDirectory() as temp:
        with patch.object(tm, "_jev_choose", side_effect=lambda task, cs, **kw: cs[:2]), \
             patch.object(tm, "_candidate_has_quota", return_value=True), \
             patch.object(tm, "_run_candidate", side_effect=outcomes) as run, \
             patch.object(tm.ai_memory_bridge, "read_context", return_value=(f"Code task {task_id} attempt 2", 1)), \
             patch.object(tm, "_jev_handoff", return_value=True) as handoff:
            result = tm._external_verification(task_id, Path(temp), "review", [], routes, routes[0], None, 2)
    assert run.call_count == (3 if memory_ok else 1)
    assert result["passed"] is memory_ok
    handoff.assert_not_called()
    if memory_ok:
        assert [c.kwargs["attempt_index"] for c in run.call_args_list] == [2, 3, 4]
        assert result["last_attempt"] == 4


def test_review_does_not_retry_missing_checkpoint():
    routes = [{"cli": cli, "model": cli} for cli in ("codex", "kimi", "agy")]
    task_id = "d" * 32
    with tempfile.TemporaryDirectory() as temp:
        with patch.object(tm, "_candidate_has_quota", return_value=True), \
             patch.object(tm, "_jev_choose", side_effect=lambda task, cs, **kw: cs[:2]), \
             patch.object(tm, "_run_candidate", return_value={"outcome": "quota_limited", "ai_memory": {"workstream_id": "stream"}}) as run, \
             patch.object(tm.ai_memory_bridge, "read_context", return_value=("unrelated historical event", 1)), \
             patch.object(tm, "_jev_handoff") as handoff:
            result = tm._external_verification(task_id, Path(temp), "review", [], routes, routes[0], None, 2)
    assert run.call_count == 1
    assert not result["passed"]
    assert result["worker_outcome"] == "memory_blocked"
    handoff.assert_not_called()


def test_review_memory_blocked_record_carries_stderr_and_stdout_excerpts():
    routes = [{"cli": cli, "model": cli, "effort": None} for cli in ("agy", "kimi")]
    task_id = "f" * 32
    # Simulate a review worker where ai-memory fails to verify attempt
    outcome = {
        "outcome": "memory_blocked",
        "reason": "ai-memory did not persist the current attempt; failover stopped",
        "stderr": "line 1\nerror: hook failed to capture session bearer token123456\nline 3" + ("x" * 600),
        "stdout": "line 1\nreview output here api_key=secret987654321\nline 3" + ("y" * 600),
        "ai_memory": None,
    }
    with tempfile.TemporaryDirectory() as temp:
        with patch.object(tm, "_candidate_has_quota", return_value=True), \
             patch.object(tm, "_jev_choose", side_effect=lambda task, cs, **kw: cs[:2]), \
             patch.object(tm, "_run_candidate", return_value=outcome) as run:
            result = tm._external_verification(task_id, Path(temp), "review", [], routes, routes[0], None, 2)
    assert run.call_count == 1
    assert not result["passed"]
    assert result["worker_outcome"] == "memory_blocked"
    reviews = result.get("reviews") or []
    assert len(reviews) == 1
    review = reviews[0]
    assert review["worker_outcome"] == "memory_blocked"
    assert "attempt_stderr" in review
    assert "attempt_stdout" in review
    assert len(review["attempt_stderr"]) <= 500
    assert len(review["attempt_stdout"]) <= 500
    # Sanitization checks
    assert "bearer [redacted]" in review["attempt_stderr"]
    assert "api_key=[redacted]" in review["attempt_stdout"]
    assert "token123456" not in review["attempt_stderr"]
    assert "secret987654321" not in review["attempt_stdout"]


def test_work_memory_blocked_record_carries_bounded_sanitized_output(tmp_path):
    task_id = "a" * 32
    outcome = {
        "outcome": "memory_blocked",
        "reason": "ai-memory did not confirm the attempt",
        "stderr": "hook failed with bearer token123456\n" + ("x" * 600),
        "stdout": "worker emitted api_key=secret987654321\n" + ("y" * 600),
        "ai_memory": None,
    }
    route = {"cli": "agy", "provider": "antigravity", "model": "gemini-3.8-flash-low",
             "effort": "low"}
    with patch.object(tm, "_root", return_value=tmp_path), \
         patch.object(tm, "_candidate_has_quota", return_value=True), \
         patch.object(tm, "_git_readonly") as git, \
         patch.object(tm, "_git_changed_paths", return_value=[]), \
         patch.object(tm, "_independent_checks", return_value={"changed_files": []}), \
         patch.object(tm.ai_memory_bridge, "verify_runtime"), \
         patch.object(tm, "_run_candidate", return_value=outcome):
        git.return_value.stdout = str(tmp_path)
        tm._write(task_id, {"task_id": task_id, "state": "queued"})
        tm._worker(task_id, tmp_path, "task", "change", [], [route], None)
        data = tm._read(task_id)
    assert data["state"] == "blocked"
    attempt = data["attempts"][0]
    assert attempt["outcome"] == "memory_blocked"
    assert len(attempt["attempt_stderr"]) <= 500
    assert len(attempt["attempt_stdout"]) <= 500
    assert "bearer [redacted]" in attempt["attempt_stderr"]
    assert "api_key=[redacted]" in attempt["attempt_stdout"]
    assert "token123456" not in attempt["attempt_stderr"]
    assert "secret987654321" not in attempt["attempt_stdout"]


def test_review_rechecks_reserve_between_passes():
    routes = [{"cli": cli, "model": cli} for cli in ("codex", "kimi", "agy")]
    finished = []
    def quota(route):
        return not (finished and route["cli"] == "kimi")
    def run(route, *args, **kwargs):
        finished.append(route["cli"])
        return {"outcome": "needs_verification", "summary": "VERDICT: PASS"}
    with tempfile.TemporaryDirectory() as temp:
        with patch.object(tm, "_candidate_has_quota", side_effect=quota), \
             patch.object(tm, "_jev_choose", side_effect=lambda task, cs, **kw: cs[:2]) as choose, \
             patch.object(tm, "_run_candidate", side_effect=run):
            result = tm._external_verification("e" * 32, Path(temp), "review", [], routes, routes[0], None, 2)
    assert finished == ["codex", "agy"]
    assert choose.call_args.kwargs["_position"] == 1
    assert result["passed"]


def test_worker_rotates_exhausted_subscription_with_same_workstream():
    routes = [{"cli": cli, "model": model, "effort": None} for cli, model in
              [("codex", "one"), ("codex", "two"), ("kimi", "three")]]
    task_id = "c" * 32
    memory = {"workstream_id": "stream", "latest_sequence": 1}
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        with patch.object(tm, "_root", return_value=root), \
             patch.object(tm, "_candidate_has_quota", return_value=True), \
             patch.object(tm, "_git_readonly") as git, \
             patch.object(tm, "_git_changed_paths", return_value=[]), \
             patch.object(tm, "_independent_checks", return_value={"changed_files": []}), \
             patch.object(tm.ai_memory_bridge, "verify_runtime"), \
             patch.object(tm.ai_memory_bridge, "read_context", return_value=(f"Code task {task_id} attempt 1", 1)), \
             patch.object(tm, "_jev_handoff", return_value=True) as handoff, \
             patch.object(tm, "_run_candidate", side_effect=[
                 {"outcome": "quota_limited", "ai_memory": memory},
                 {"outcome": "account_stop", "ai_memory": memory},
             ]) as run:
            git.return_value.stdout = str(root)
            tm._write(task_id, {"task_id": task_id, "state": "queued"})
            tm._worker(task_id, root, "task", "change", [], routes, None)
            assert [c.args[0]["cli"] for c in run.call_args_list] == ["codex", "kimi"]
            assert handoff.call_args.args[3] == routes[2]
            assert "attempt 1" in run.call_args_list[1].kwargs["memory_context"]


@pytest.mark.parametrize("error", ["HTTP 429 quota limit", "quota exceeded", "insufficient quota", "Too many requests"])
def test_quota_failure_is_retryable(error):
    assert tm._failure_category("", error) == "quota_limited"


@pytest.mark.parametrize("error", ["HTTP 401 authentication failed with 429", "unauthorized", "billing limit", "permission denied"])
def test_auth_billing_permissions_never_become_quota_retry(error):
    assert tm._failure_category("", error) not in {"quota_limited", "model_rate_limited"}
