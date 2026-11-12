"""AGY participates fully in managed ai-memory attempts and handoffs."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, call

import pytest

import task_manager as tm


AGY = {"cli": "agy", "provider": "antigravity", "model": "gemini-3.8-flash-low", "effort": "low"}
CODEX = {"cli": "codex", "provider": "openai-codex", "model": "gpt-6-luna", "effort": "low"}
KIMI = {"cli": "kimi", "provider": "kimi", "model": "kimi-for-coding", "effort": None}
TASK_ID = "a" * 32


def test_agy_candidate_memory_mode():
    agy = dict(AGY)
    with patch.object(tm.shutil, "which", return_value="/usr/bin/bwrap"):
        assert agy in tm._worker_capable([agy])
    assert tm._candidate_memory_mode(agy, "managed") == "managed"
    assert tm._candidate_memory_mode(CODEX, "managed") == "managed"
    assert tm._candidate_memory_mode(agy, "standalone") == "standalone"


def test_agy_success_is_recorded_with_managed_memory(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    checks = {"checks": {"diff": {"ok": True}}, "changed_files": ["change.py"],
              "head_unchanged": False}
    with patch.object(tm, "_root", return_value=tmp_path / "state"), \
         patch.object(tm, "_lock_path", return_value=tmp_path / "repo.lock"), \
         patch.object(tm, "_candidate_has_quota", return_value=True), \
         patch.object(tm, "_git_readonly", return_value=SimpleNamespace(stdout="head")), \
         patch.object(tm, "_git_changed_paths", return_value=[]), \
         patch.object(tm, "_path_digests", return_value={}), \
         patch.object(tm, "_independent_checks", return_value=checks), \
         patch.object(tm, "_acceptance", return_value={"passed": True}), \
         patch.object(tm, "_independent_review_enabled", return_value=False), \
         patch.object(tm.ai_memory_bridge, "verify_runtime"), \
         patch.object(tm.ai_memory_bridge, "verify_attempt", return_value=1), \
         patch.object(tm, "_run_candidate", return_value={
             "outcome": "needs_verification", "summary": "done",
             "ai_memory": {"workstream_id": "stream", "sequence": 1},
         }) as run:
        tm._write(TASK_ID, {"task_id": TASK_ID, "state": "queued", "ai_memory": {"mode": "managed"}})
        tm._worker(TASK_ID, repo, "inspect and fix", "change", [], [AGY], None)
        result = tm._read(TASK_ID)
    assert result["state"] == "completed"
    assert run.call_args.kwargs["memory_mode"] == "managed"
    attempt = result["attempts"][0]
    assert attempt["outcome"] == "verified"
    assert attempt["memory_policy"] == "managed"
    assert attempt["ai_memory"] == {"workstream_id": "stream", "sequence": 1}
    assert result["ai_memory"]["last_attempt_marker"] == f"Code task {TASK_ID} attempt 1"


def test_agy_worker_failure_hands_off_to_subsequent_candidate(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    checks = {"checks": {"diff": {"ok": True}}, "changed_files": ["change.py"],
              "head_unchanged": False}
    with patch.object(tm, "_root", return_value=tmp_path / "state"), \
         patch.object(tm, "_lock_path", return_value=tmp_path / "repo.lock"), \
         patch.object(tm, "_candidate_has_quota", return_value=True), \
         patch.object(tm, "_git_readonly", return_value=SimpleNamespace(stdout="head")), \
         patch.object(tm, "_git_changed_paths", return_value=[]), \
         patch.object(tm, "_path_digests", return_value={}), \
         patch.object(tm, "_independent_checks", return_value=checks), \
         patch.object(tm, "_acceptance", return_value={"passed": True}), \
         patch.object(tm, "_independent_review_enabled", return_value=False), \
         patch.object(tm.ai_memory_bridge, "verify_runtime"), \
         patch.object(tm.ai_memory_bridge, "verify_attempt", return_value=1), \
         patch.object(tm.ai_memory_bridge, "read_context_with_wait",
                      return_value=(f"Code task {TASK_ID} attempt 1", 1)) as read_context, \
         patch.object(tm, "_jev_handoff", return_value=True) as handoff, \
         patch.object(tm, "_run_candidate", side_effect=[
             {"outcome": "quota_limited", "ai_memory": {"workstream_id": "stream", "sequence": 1}},
             {"outcome": "needs_verification", "summary": "done",
              "ai_memory": {"workstream_id": "stream", "sequence": 2}},
         ]) as run:
        tm._write(TASK_ID, {"task_id": TASK_ID, "state": "queued", "ai_memory": {"mode": "managed"}})
        tm._worker(TASK_ID, repo, "inspect and fix", "change", [], [AGY, CODEX], None)
        result = tm._read(TASK_ID)

    assert result["state"] == "completed"
    assert run.call_count == 2
    assert [call.args[0]["cli"] for call in run.call_args_list] == ["agy", "codex"]
    read_context.assert_called_with(
        repo, "stream", required_marker=f"Code task {TASK_ID} attempt 1")
    handoff.assert_called_once()


def test_agy_review_failure_rotates_to_next_reviewer(tmp_path):
    routes = [AGY, CODEX, KIMI]
    task_id = "b" * 32
    with patch.object(tm, "_candidate_has_quota", return_value=True), \
         patch.object(tm, "_jev_choose", side_effect=lambda _task, candidates, **_kwargs: candidates), \
         patch.object(tm, "_run_candidate", side_effect=[
             {"outcome": "quota_limited", "ai_memory": {"workstream_id": "stream"}},
             {"outcome": "needs_verification", "summary": "VERDICT: PASS",
              "ai_memory": {"workstream_id": "stream"}},
             {"outcome": "needs_verification", "summary": "VERDICT: PASS",
              "ai_memory": {"workstream_id": "stream"}},
         ]) as run, \
         patch.object(tm.ai_memory_bridge, "read_context_with_wait",
                      return_value=(f"Code task {task_id} attempt 1", 1)) as read_context:
        review = tm._external_verification(task_id, tmp_path, "review", [], routes, CODEX, None, 1,
                                           memory_mode="managed")
    assert run.call_count == 3
    assert [call.args[0]["cli"] for call in run.call_args_list] == ["agy", "codex", "kimi"]
    assert review["passed"] is True
    assert review["complete"] is True
    read_context.assert_called_once_with(
        tmp_path, "stream", required_marker=f"Code task {task_id} attempt 1")


def test_agy_fail_verdict_triggers_repair_worker(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    checks = {"checks": {"diff": {"ok": True}}, "changed_files": ["change.py"],
              "head_unchanged": False}
    routes = [CODEX, AGY, KIMI]
    with patch.object(tm, "_root", return_value=tmp_path / "state"), \
         patch.object(tm, "_lock_path", return_value=tmp_path / "repo.lock"), \
         patch.object(tm, "_candidate_has_quota", return_value=True), \
         patch.object(tm, "_git_readonly", return_value=SimpleNamespace(stdout="head")), \
         patch.object(tm, "_git_changed_paths", return_value=[]), \
         patch.object(tm, "_path_digests", return_value={}), \
         patch.object(tm, "_independent_checks", return_value=checks), \
         patch.object(tm, "_acceptance", return_value={"passed": True}), \
         patch.object(tm.ai_memory_bridge, "verify_runtime"), \
         patch.object(tm.ai_memory_bridge, "verify_attempt", return_value=1), \
         patch.object(tm, "_external_verification", return_value={
             "passed": False, "failed": True, "summary": "VERDICT: FAIL", "reviews": [],
         }), \
         patch.object(tm.ai_memory_bridge, "read_context_with_wait",
                      return_value=(f"Code task {TASK_ID} attempt 1", 1)), \
         patch.object(tm, "_jev_handoff", return_value=True), \
         patch.object(tm, "_run_candidate", side_effect=[
             {"outcome": "needs_verification", "summary": "attempt 1 implemented",
              "ai_memory": {"workstream_id": "stream", "sequence": 1}},
             {"outcome": "needs_verification", "summary": "attempt 2 fixed",
              "ai_memory": {"workstream_id": "stream", "sequence": 2}},
         ]) as run:
        # Second run should verify without external review
        with patch.object(tm, "_independent_review_enabled", side_effect=[True, False]):
            tm._write(TASK_ID, {"task_id": TASK_ID, "state": "queued", "ai_memory": {"mode": "managed"}})
            tm._worker(TASK_ID, repo, "inspect and fix", "change", [], routes, None)
            result = tm._read(TASK_ID)

    assert run.call_count == 2
    assert len(result["attempts"]) == 2
    assert result["attempts"][0]["outcome"] == "failed_verification"
    assert result["attempts"][1]["outcome"] == "verified"
    assert result["state"] == "completed"


def test_model_discovery_lists_agy_models():
    listing = "gemini-3.8-flash-low  Fast\ngemini-4-argon-high  Deep\nunrelated-model\n"
    with patch.object(tm.shutil, "which", side_effect=lambda name, *a, **k: "/usr/bin/agy" if name == "agy" else None), \
         patch.object(tm, "_installed_cli", return_value=None), \
         patch.object(tm.subprocess, "run", return_value=SimpleNamespace(stdout=listing, returncode=0)):
        models = tm._installed_models()
    assert models["agy"] == {"gemini-3.8-flash-low": ["low"], "gemini-4-argon-high": ["high"]}


def test_status_view_keeps_long_summaries():
    long_summary = "x" * 2500 + "\nVERDICT: PASS"
    view = tm._status_view({"task_id": TASK_ID, "state": "succeeded", "summary": long_summary,
                            "events": []}, include_history=True)
    assert view["summary"].endswith("VERDICT: PASS")
