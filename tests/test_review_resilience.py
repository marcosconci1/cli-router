import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import task_manager as tm


MEMORY = {"workstream_id": "stream", "latest_sequence": 1}


def route(cli, model, account="account"):
    provider = {"agy": "antigravity", "codex": "openai-codex"}.get(cli, cli)
    return {"cli": cli, "provider": provider, "account": account,
            "model": model, "effort": model.rsplit("-", 1)[-1] if cli == "agy" else None}


def run_worker(routes, outcomes):
    task_id = "a" * 32
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        with patch.object(tm, "_root", return_value=root), \
             patch.object(tm, "_candidate_has_quota", return_value=True), \
             patch.object(tm, "_git_readonly") as git, \
             patch.object(tm, "_git_changed_paths", return_value=[]), \
             patch.object(tm, "_independent_checks", return_value={"changed_files": []}), \
             patch.object(tm.ai_memory_bridge, "verify_runtime"), \
             patch.object(tm.ai_memory_bridge, "read_context",
                          return_value=(f"Code task {task_id} attempt 1", 1)), \
             patch.object(tm, "_jev_handoff", return_value=True), \
             patch.object(tm, "_run_candidate", side_effect=outcomes) as run:
            git.return_value.stdout = str(root)
            tm._write(task_id, {"task_id": task_id, "state": "queued"})
            tm._worker(task_id, root, "task", "change", [], routes, None)
    return run


def test_agy_worker_quota_failure_stops_without_another_route():
    routes = [route("agy", "gemini-4-argon-medium"),
              route("agy", "gemini-4-argon-low"),
              route("agy", "gemini-3.8-flash-low")]
    run = run_worker(routes, [
        {"outcome": "quota_limited", "ai_memory": MEMORY},
        {"outcome": "account_stop", "ai_memory": MEMORY},
    ])
    assert [call.args[0]["model"] for call in run.call_args_list] == ["gemini-4-argon-medium"]
    assert run.call_args.kwargs["memory_mode"] == "standalone"


@pytest.mark.parametrize("cli", ["codex", "kimi"])
def test_non_agy_429_remains_account_wide(cli):
    routes = [route(cli, "one"), route(cli, "two"), route("agy", "gemini-3.8-flash-low", "other")]
    run = run_worker(routes, [
        {"outcome": "quota_limited", "ai_memory": MEMORY},
        {"outcome": "account_stop", "ai_memory": MEMORY},
    ])
    assert [call.args[0] for call in run.call_args_list] == [routes[0], routes[2]]


def test_agy_review_quota_failure_stops_review_rotation():
    routes = [route("agy", "gemini-4-argon-medium"),
              route("agy", "gemini-3.8-flash-low"),
              route("kimi", "kimi-code/k3.1-pro", "other")]
    task_id = "b" * 32
    with tempfile.TemporaryDirectory() as temp:
        with patch.object(tm, "_jev_choose", side_effect=lambda task, candidates, **kwargs: candidates[:2]), \
             patch.object(tm, "_candidate_has_quota", return_value=True), \
             patch.object(tm, "_run_candidate", side_effect=[
                 {"outcome": "quota_limited", "ai_memory": MEMORY},
                 {"outcome": "needs_verification", "summary": "VERDICT: PASS", "ai_memory": MEMORY},
                 {"outcome": "needs_verification", "summary": "VERDICT: PASS", "ai_memory": MEMORY},
             ]) as run, \
             patch.object(tm.ai_memory_bridge, "read_context_with_wait",
                          return_value=(f"Code task {task_id} attempt 2", 1)) as read_context, \
             patch.object(tm, "_jev_handoff") as handoff:
            result = tm._external_verification(task_id, Path(temp), "review", [], routes, routes[0], None, 2)
    assert [call.args[0]["model"] for call in run.call_args_list] == ["gemini-4-argon-medium"]
    assert run.call_args.kwargs["memory_mode"] == "managed"  # launcher enforces AGY's standalone policy
    assert not result["complete"]
    read_context.assert_not_called()
    handoff.assert_not_called()


def test_review_missing_marker_still_stops_without_calling_jev():
    routes = [route("codex", "one"), route("kimi", "two", "other")]
    task_id = "c" * 32
    with tempfile.TemporaryDirectory() as temp:
        with patch.object(tm, "_jev_choose", side_effect=lambda task, candidates, **kwargs: candidates), \
             patch.object(tm, "_candidate_has_quota", return_value=True), \
             patch.object(tm, "_run_candidate",
                          return_value={"outcome": "quota_limited", "ai_memory": MEMORY}) as run, \
             patch.object(tm.ai_memory_bridge, "read_context", return_value=("unrelated event", 1)), \
             patch.object(tm, "_jev_handoff") as handoff:
            result = tm._external_verification(task_id, Path(temp), "review", [], routes, routes[0], None, 2)
    assert run.call_count == 1
    assert result["worker_outcome"] == "memory_blocked"
    handoff.assert_not_called()


def run_reviewed_worker(external):
    task_id = "d" * 32
    routes = [route("codex", "one"), route("kimi", "two", "other"),
              route("agy", "gemini-3.8-flash-low", "third")]
    outcome = {"outcome": "needs_verification", "summary": "implemented", "ai_memory": MEMORY}
    checks = {"checks": {"git_diff_clean": {"ok": True}},
              "changed_files": ["done.py"], "head_unchanged": False}
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        head_before = str(root)
        with patch.object(tm, "_root", return_value=root), \
             patch.object(tm, "_candidate_has_quota", return_value=True), \
             patch.object(tm, "_git_readonly") as git, \
             patch.object(tm, "_git_changed_paths", return_value=[]), \
             patch.object(tm, "_independent_checks", return_value=checks), \
             patch.object(tm.ai_memory_bridge, "verify_runtime"), \
             patch.object(tm.ai_memory_bridge, "read_context",
                          return_value=(f"Code task {task_id} attempt 1", 1)), \
             patch.object(tm, "_run_candidate", return_value=outcome) as run, \
             patch.object(tm, "_external_verification", return_value=external) as verify, \
             patch.object(tm, "_jev_handoff", return_value=True) as handoff:
            git.return_value.stdout = head_before
            tm._write(task_id, {"task_id": task_id, "state": "queued"})
            tm._worker(task_id, root, "task", "change",
                       [{"type": "file_changed", "path": "done.py"}], routes, None)
            data = tm._read(task_id)
    return data, run, verify, handoff, head_before


def test_unreviewed_is_terminal_and_triggers_no_repair():
    data, run, verify, handoff, head_before = run_reviewed_worker({
        "passed": False, "complete": False, "failed": False, "summary": "review incomplete",
    })
    assert data["state"] == "unreviewed"
    assert data["attempts"][0]["outcome"] == "unreviewed"
    assert data["verification"]["changed_files"] == ["done.py"]
    assert data["base_head"] == head_before
    assert "without repair" in data["message"]
    status = tm._status_view(data)
    assert status["state"] == "unreviewed"
    assert "next_action" not in status
    assert run.call_count == 1
    assert verify.call_count == 1
    handoff.assert_not_called()


def test_real_fail_verdict_stays_failed_verification():
    data, run, verify, handoff, _ = run_reviewed_worker({
        "passed": False, "complete": True, "failed": True, "summary": "VERDICT: FAIL",
    })
    assert data["state"] == "failed_verification"
    assert run.call_count == 2
    assert verify.call_count == 2
    assert handoff.call_count == 1


def test_report_counts_unreviewed_separately(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts" / "cli-task-models.py"
    spec = importlib.util.spec_from_file_location("cli_router_models", script)
    report = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(report)
    for task_id, state in [("e" * 32, "unreviewed"), ("f" * 32, "failed_verification")]:
        (tmp_path / f"{task_id}.json").write_text(json.dumps({"task_id": task_id, "state": state}))
    tasks = report.load_tasks(tmp_path)
    states, _, _ = report.totals(tasks)
    assert states["unreviewed"] == 1
    assert states["failed_verification"] == 1
    rendered = report.render(tasks, 10, None)
    assert "1 unreviewed" in rendered
    assert "1 failed_verification" in rendered
